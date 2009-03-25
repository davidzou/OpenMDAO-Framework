__all__ = ('CaseIteratorDriver', 'ICaseRecorder')
__version__ = '0.1'

import Queue
import threading
import time

from openmdao.main import Driver, Bool
from openmdao.main.component import RUN_OK, RUN_FAILED, RUN_STOPPED, RUN_UNKNOWN
from openmdao.main.interfaces import ICaseIterator
from openmdao.main.variable import INPUT


SERVER_EMPTY    = 1
SERVER_READY    = 2
SERVER_COMPLETE = 3
SERVER_ERROR    = 4

class ServerError(Exception):
    pass


class CaseIteratorDriver(Driver):
    """
    Run a set of cases provided by an ICaseIterator in a manner similar
    to the ROSE framework.

    TODO: support concurrent evaluation.
    """

    def __init__(self, name, parent=None, doc=None):
        super(CaseIteratorDriver, self).__init__(name, parent, doc)

        Bool('sequential', self, INPUT, default=True,
             doc='Evaluate cases sequentially.')

        Bool('reload_model', self, INPUT, default=True,
             doc='Reload model between executions.')

        self.add_socket('iterator', ICaseIterator, 'Cases to evaluate.')
        self.add_socket('outerator', None, 'Something to append() to.')

        self._iter = None
        self._n_servers = 0
        self._model_file = None
        self._reply_queue = None
        self._server_lock = None
        self._servers = {}  # Server information, keyed by name.
        self._queues = {}   # Request queues, keyed by server name.
        self._in_use = {}   # In-use flags, keyed by server name.

        self._server_states = {}
        self._server_cases = {}
        self._rerun = []

    def execute(self):
        """ Run each case in iterator and record results in outerator. """
        if not self.check_socket('iterator'):
            self.error('No iterator plugin')
            return RUN_FAILED

        if not self.check_socket('outerator'):
            self.error('No outerator plugin')
            return RUN_FAILED

        self._rerun = []
        self._iter = self.iterator.__iter__()

        if self.sequential or self._n_servers < 1:
            self.info('Start sequential evaluation.')
            while self._server_ready(None):
                pass
        else:
            self.info('Start concurrent evaluation, n_servers %d',
                      self._n_servers)
            self.raise_exception('Concurrent evaluation is not supported yet.',
                                 NotImplementedError)

            # Replicate model and save to file.
            # Must do this before creating any locks or queues.
            replicant = self.parent.replicate()
            self._model_file = 'replicant.dam'
            replicant.save(self._model_file)
            del replicant

            # Start servers.
            self._server_lock = threading.Lock()
            self._reply_queue = Queue.Queue()
            for i in range(self._n_servers):
                name = 'cid_%d' % (i+1)
                server_thread = threading.Thread(target=self._service_loop,
                                                 args=(name,))
                server_thread.setDaemon(True)
                server_thread.start()
                time.sleep(0.1)  # Pacing for GX at least.

            for i in range(self._n_servers):
                name, status = self._reply_queue.get()
            if len(self._servers) > 0:
                if len(self._servers) < self._n_servers:
                    self.warning('Only %d servers created', len(self._servers))
            else:
                self.raise_exception('No servers created!', RuntimeError)

            # Kick-off initial state.
            for name in self._servers.keys():
                self._in_use[name] = self._server_ready(name)

            # Continue until no servers are busy.
            while self._busy():
                name, result = self._reply_queue.get()
                self._in_use[name] = self._server_ready(name)

            # Shut-down servers.
            for name in self._servers.keys():
                self._queues[name].put(None)
            for i in range(len(self._servers)):
                name, status = self._reply_queue.get()

            # Clean up.
            self._reply_queue = None
            self._server_lock = None
            self._servers = {}
            self._queues = {}
            self._in_use = {}

        if self._stop:
            return RUN_STOPPED
        else:
            return RUN_OK

    def _server_ready(self, server):
        """
        Responds to asynchronous callbacks during execute() to run cases
        retrieved from Iterator.  Results are processed by Outerator.
        Returns True if there are more cases to run.
        """
# TODO: improve response to a stop request.
        server_state = self._server_states.get(server, SERVER_EMPTY)
        if server_state == SERVER_EMPTY:
            try:
                self.load_model(server)
                self._server_states[server] = SERVER_READY
                return True
            except ServerError:
                self._server_states[server] = SERVER_ERROR
                return True

        elif server_state == SERVER_READY:
            # Test for stop request.
            if self._stop:
                return False
            # Check if there are cases that need to be rerun.
            if self._rerun:
                self.debug('_rerun: %s', repr(self._rerun))
                self._run_case(self._rerun.pop(0), server)
                return True
            else:
                # Try to get a new case.
                try:
                    case = self._iter.next()
                except StopIteration:
                    return False
                else:
                    case.status = RUN_UNKNOWN
                    self._run_case(case, server)
                    return True
        
        elif server_state == SERVER_COMPLETE:
            try:
                case = self._server_cases[server]
                # Grab the data from the model.
                case.status = self.model_status(server)
                for i, niv in enumerate(case.outputs):
                    try:
                        case.outputs[i] = (niv[0], niv[1],
                            self.model_get(server, niv[0], niv[1]))
                    except Exception, exc:
                        msg = "Exception getting '%s': %s" % (niv[0], str(exc))
                        self.error(msg)
                        case.status = RUN_FAILED
                        case.msg = '%s: %s' % (self.get_pathname(), msg)
                # Record the data.
                self.outerator.append(case)

                if case.status == RUN_OK:
                    if self.reload_model:
                        self.model_cleanup(server)
                        self.load_model(server)
                else:
                    self.load_model(server)
                self._server_states[server] = SERVER_READY
                return True
            except ServerError:
                # Handle server error separately.
                return True
        
        elif server_state == SERVER_ERROR:
            try:
                self.load_model(server)
            except ServerError:
                return True
            else:
                self._server_states[server] = SERVER_READY
                return True

    def _run_case(self, case, server):
        """ Setup and run a case. """
        try:
            self._server_cases[server] = case
            for name, index, value in case.inputs:
                try:
                    self.model_set(server, name, index, value)
                except Exception, exc:
                    msg = "Exception setting '%s': %s" % (name, str(exc))
                    self.error(msg)
                    self.raise_exception(msg, ServerError)
            self.model_execute(server)
            self._server_states[server] = SERVER_COMPLETE
        except ServerError, exc:
            self._server_states[server] = SERVER_ERROR
            if case.status == RUN_UNKNOWN:
                case.status = RUN_FAILED
                self._rerun.append(case)  # Try one more time.
            else:
                case.status = RUN_FAILED
                case.msg = str(exc)
                self.outerator.append(case)

    def _service_loop(self, name):
        """ Each server has an associated thread executing this. """
#        ram = da.Simulation.get_simulation().ram
        ram = None
        server, server_info = ram.allocate({}, transient=True)
        if server is None:
            self.error('Server allocation for %s failed :-(', name)
            self._reply_queue.put((name, False))
            return

# TODO: external files should be part of saved state.
        ram.walk(self.parent, server)

        request_queue = Queue.Queue()

        self._server_lock.acquire()
        self._servers[name] = server
        self._queues[name] = request_queue
        self._in_use[name] = False
        self._server_lock.release()

        self._reply_queue.put((name, True))

        while True:
            request = request_queue.get()
            if request is None:
                break
            result = request[0](request[1])
            self._reply_queue.put((name, result))

        ram.release(server)
        self._reply_queue.put((name, True))

    def _busy(self):
        """ Return True while at least one server is in use. """
        for name in self._servers.keys():
            if self._in_use[name]:
                return True
        return False

    def load_model(self, server):
        """ Load a model into a server. """
        if server is not None:
            self._queues[server].put((self._load_model, server))
        return True

    def _load_model(self, server):
        """ Load model into server. """
# TODO: use filexfer() utility.
        inp = open(self._model_file, 'rb')
        out = self._servers[server].open(self._model_file, 'wb')
        chunk = 1 << 17    # 128KB
        data = inp.read(chunk)
        while data:
            out.write(data)
            data = inp.read(chunk)
        inp.close()
        out.close()
        if not self._servers[server].load_model(self._model_file):
            self.error('server.load_model failed :-(')
            return False
        return True

    def model_set(self, server, name, index, value):
        """ Set value in server's model. """
        comp_name, attr = name.split('.', 1)
        if server is None:
            comp = getattr(self.parent, comp_name)
        else:
            comp = getattr(self._servers[server].tla, comp_name)
        comp.set(attr, value, index)
        return True

    def model_get(self, server, name, index):
        """ Get value from server's model. """
        comp_name, attr = name.split('.', 1)
        if server is None:
            comp = getattr(self.parent, comp_name)
        else:
            comp = getattr(self._servers[server].tla, comp_name)
        return comp.get(attr, index)

    def model_execute(self, server):
        """ Execute model in server. """
        if server is None:
            self.parent.workflow.run()
        else:
            self._queues[server].put((self._model_execute, server))

    def _model_execute(self, server):
        """ Execute model. """
        self._servers[server].tla.run()

    def model_status(self, server):
        """ Return execute status from model. """
        if server is None:
            return self.parent.workflow.execute_status
        else:
            return self._servers[server].tla.execute_status

    def model_cleanup(self, server):
        """ Clean up model resources. """
        return True


