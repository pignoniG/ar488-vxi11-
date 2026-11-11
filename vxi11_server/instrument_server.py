# MIT License

# Copyright (c) [2019] [Coburn Wightman]

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.


from . import rpc
from . import vxi11

#import os
import logging
import threading
import socketserver
import collections
import copy
import socket
import time
from contextlib import contextmanager

from . import instrument_device as Instrument

MAX_RECEIVE_SIZE = 16384

logger = logging.getLogger(__name__)

class Error(object):
    '''vxi11 specification error constants'''
    NO_ERROR = vxi11.ERR_NO_ERROR
    SYNTAX_ERROR = vxi11.ERR_SYNTAX_ERROR
    DEVICE_NOT_ACCESSIBLE = vxi11.ERR_DEVICE_NOT_ACCESSIBLE
    INVALID_LINK_IDENTIFIER = vxi11.ERR_INVALID_LINK_IDENTIFIER
    PARAMETER_ERROR = vxi11.ERR_PARAMETER_ERROR
    CHANNEL_NOT_ESTABLISHED = vxi11.ERR_CHANNEL_NOT_ESTABLISHED
    OPERATION_NOT_SUPPORTED = vxi11.ERR_OPERATION_NOT_SUPPORTED
    OUT_OF_RESOURCES = vxi11.ERR_OUT_OF_RESOURCES
    DEVICE_LOCKED_BY_ANOTHER_LINK = vxi11.ERR_DEVICE_LOCKED_BY_ANOTHER_LINK
    NO_LOCK_HELD_BY_THIS_LINK = vxi11.ERR_NO_LOCK_HELD_BY_THIS_LINK
    IO_TIMEOUT = vxi11.ERR_IO_TIMEOUT
    IO_ERROR = vxi11.ERR_IO_ERROR
    INVALID_ADDRESS = vxi11.ERR_INVALID_ADDRESS
    ABORT = vxi11.ERR_ABORT
    CHANNEL_ALREADY_ESTABLISHED = vxi11.ERR_CHANNEL_ALREADY_ESTABLISHED

class Flags(object):
    WAITLOCK = vxi11.OP_FLAG_WAIT_BLOCK
    END = vxi11.OP_FLAG_END
    TERMCHARSET = vxi11.OP_FLAG_TERMCHAR_SET
    
class LockedIncrementer(object):
    _value = 0 #private global.

    #contain a list of active sessions also?
    #client_id = random.getrandbits(31)
    def __init__(self, start_value):
        self.lock = threading.Lock()
        self._value = start_value
                
    def next(self):
        self.lock.acquire()
        try:
            self._value = self._value + 1
            return self._value
        finally:
            self.lock.release()

class DeviceLock(object):
    """VXI-11 exclusive lock (device_lock / device_unlock / lock_device on create_link).

    Patched for ar488-vxi11: the original used one non-blocking RLock both for
    explicit locks and for every operation, so two links talking to the same
    device at the same time got spurious ERR_DEVICE_LOCKED_BY_ANOTHER_LINK.
    Now only explicit locks exclude other links; serialising bus access is the
    job of the device handler.
    """
    def __init__(self, device_name):
        self.device_name = device_name
        self.cond = threading.Condition()
        self.lock_id = 0
        return

    def _wait_free(self, link_id, flags, lock_timeout):
        # caller holds self.cond
        if self.lock_id in (0, link_id):
            return True
        if not (flags & Flags.WAITLOCK):
            return False
        return self.cond.wait_for(lambda: self.lock_id in (0, link_id), lock_timeout / 1000)

    def acquire(self, link_id, flags, lock_timeout):
        logger.debug('locking device: %s', self.device_name)
        with self.cond:
            if self._wait_free(link_id, flags, lock_timeout):
                self.lock_id = link_id
                return vxi11.ERR_NO_ERROR
        return vxi11.ERR_DEVICE_LOCKED_BY_ANOTHER_LINK

    def release(self, link_id):
        logger.debug('unlocking device: %s', self.device_name)
        with self.cond:
            if self.lock_id == link_id and link_id != 0:
                self.lock_id = 0
                self.cond.notify_all()
                return vxi11.ERR_NO_ERROR
        return vxi11.ERR_NO_LOCK_HELD_BY_THIS_LINK

    @contextmanager
    def __call__(self, link_id, flags, lock_timeout):
        with self.cond:
            ok = self._wait_free(link_id, flags, lock_timeout)
        yield vxi11.ERR_NO_ERROR if ok else vxi11.ERR_DEVICE_LOCKED_BY_ANOTHER_LINK

class DeviceItem(object):
    def __init__(self, device_class):
        self.device_class = device_class
        self.lock = None
        return
    
class DeviceRegistry(object):
    _next_device_index = 0
    _registry = {}

    def __init__(self):
        pass
    
    def register(self, name, device_class):
        if name is None:
            while 'inst' +  str(self._next_device_index) in self._registry:
                self._next_device_index += 1
            name = 'inst' +  str(self._next_device_index)

        if name in self._registry:
            raise KeyError

        item = DeviceItem(device_class)
        item.lock = DeviceLock(name)
        
        self._registry[name] = item
        
        return
    
    def remove(self, name):
        del self._registry[name]

    def directory(self):
        return self._registry.keys()

    def factory(self, name):
        item = self._registry[name]

        device = item.device_class(name, item.lock)
        device.device_list = self.directory()
        
        return device
        
class Vxi11Server(socketserver.ThreadingMixIn, rpc.TCPServer):
    _next_device_index = 0
    _device_registry = DeviceRegistry()
    _link_registry = {}

    def __init__(self, host, prog, vers, port, handler_class):
        rpc.TCPServer.__init__(self, host, prog, vers, port, handler_class)
        self.lid_gen = LockedIncrementer(200)
        return

    # move device_class_registry, link_create/delete to CoreHandler?
    def link_create(self, device_name):        
        # create and initialize an instance of the device handler registered to device_name
        device_instance = self._device_registry.factory(device_name)
        
        # and register it to a new link_id
        link_id = self.lid_gen.next()
        self._link_registry[link_id] = device_instance
        
        return link_id, self._link_registry[link_id]
        
    def link_delete(self, link_id):
        self._link_registry.pop(link_id, None)
        return

    #def link_name(self, link_id):
    #    return self._link_registry[link_id]
    
    def link_abort(self, link_id):
        try:
            device_instance =  self._link_registry[link_id]
            logger.debug('AbortServer: ABORT_LINK_ID %s to %s', link_id, self._link_registry[link_id])
            error = device_instance.device_abort()
        except KeyError :
            logger.debug('AbortServer: ABORT_LINK_ID %s. link_id does not exist.', link_id)
            error = vxi11.ERR_INVALID_LINK_IDENTIFIER
        return error

    # should the device registry be moved to the core server?
    def device_register(self, name, device_class):
        self._device_registry.register(name, device_class)
        
    def device_unregister(self, name):
        self._device_registry.remove(name)
        return
    
    # def device_list(self):
    #     return self._device_registry.directory()
    
class Vxi11Handler(rpc.RPCRequestHandler):
    def addpackers(self):
        # amend rpc packers with our vxi11 packers
        self.packer = vxi11.Packer()
        self.unpacker = vxi11.Unpacker('')
        return
    
class Vxi11AbortServer(Vxi11Server):
    def __init__(self):
        Vxi11Server.__init__(self, '', vxi11.DEVICE_ASYNC_PROG, vxi11.DEVICE_ASYNC_VERS, 0, Vxi11AbortHandler)
        return
    
class Vxi11AbortHandler(Vxi11Handler):
    def handle_1(self):
        params = self.unpacker.unpack_device_link()
        link_id = params
        error=self.server.link_abort(link_id)

        self.turn_around()
        self.packer.pack_device_error(error)
        return
    
class Vxi11CoreServer(Vxi11Server):
    def __init__(self, abort_port):
        Vxi11Server.__init__(self, '', vxi11.DEVICE_CORE_PROG, vxi11.DEVICE_CORE_VERS, 0, Vxi11CoreHandler)
        self.abort_port = abort_port
        return


def _device_close(device):
    # optional hook (ar488-vxi11): the link is going away
    close = getattr(device, 'device_close', None)
    if close is not None:
        try:
            close()
        except Exception:
            logger.exception('device_close of %s failed', device.device_name)


# Open client connections, for monitoring (ar488-vxi11 web UI): id -> handler
CONNECTIONS = {}
CONNECTIONS_LOCK = threading.Lock()
_conn_ids = iter(range(1, 1 << 62))


# RPCs whose arguments start with a link id
LINK_PROCS = frozenset((11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 22, 23))
FINISH_WAIT_S = 30


class Vxi11CoreHandler(Vxi11Handler):
    """One instance per client TCP connection (core channel).

    Patched for ar488-vxi11:
    * A connection can carry any number of links (VXI-11 allows it, and some
      VISA libraries open all their sessions to one host over one connection).
      The original kept only the last link per connection.
    * Calls for different links on one connection run in parallel (one worker
      per link, replies matched by RPC xid); calls for the same link still run
      in the order received.
    * If the client vanishes without destroy_link, its links and locks are
      released, otherwise an explicit lock would be held forever.
    """

    device = None

    def setup(self):
        self.links = {}                        # link id -> device, shared by the workers
        self._send_lock = threading.Lock()
        self._qcond = threading.Condition()
        self._queues = {}                      # route key -> deque of pending calls
        self._busy = 0
        self.connected_at = self.last_activity = time.time()
        self.calls = 0
        with CONNECTIONS_LOCK:
            self.conn_id = next(_conn_ids)
            CONNECTIONS[self.conn_id] = self
        return super().setup()

    def disconnect(self):
        """Drop this client (from the web UI); finish() then releases its links and locks."""
        try:
            self.request.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _select(self, link_id):
        self.device = self.links.get(link_id)
        return self.device is not None

    @staticmethod
    def _route_key(call):
        try:
            u = rpc.Unpacker(call)
            for _ in range(5):                 # xid, type, rpcvers, prog, vers
                u.unpack_uint()
            proc = u.unpack_uint()
            u.unpack_auth()
            u.unpack_auth()
            if proc in LINK_PROCS:
                return u.unpack_uint()
        except Exception:
            pass
        return None                            # create_link, intr chan, null, garbage

    def handle(self):
        while True:
            try:
                call = rpc.recvrecord(self.request)
            except (EOFError, OSError):
                break
            key = self._route_key(call)
            self.last_activity = time.time()
            self.calls += 1
            with self._qcond:
                q = self._queues.get(key)
                if q is not None:
                    q.append(call)
                    continue
                q = self._queues[key] = collections.deque([call])
                self._busy += 1
            threading.Thread(target=self._worker, args=(key, q), daemon=True).start()
        # let calls still running end before finish() releases the links
        with self._qcond:
            if not self._qcond.wait_for(lambda: self._busy == 0, FINISH_WAIT_S):
                logger.warning('client %s gone, %d call(s) still running',
                               self.client_address, self._busy)

    def _worker(self, key, q):
        while True:
            with self._qcond:
                if not q:
                    del self._queues[key]
                    self._busy -= 1
                    self._qcond.notify_all()
                    return
                call = q.popleft()
            call_handler = copy.copy(self)     # own packer/unpacker, shared self.links
            call_handler.addpackers()
            try:
                reply = call_handler.handle_call(call)
            except Exception:
                logger.exception('error handling call from %s', self.client_address)
                reply = None
            if reply is not None:
                with self._send_lock:
                    try:
                        rpc.sendrecord(self.request, reply)
                    except OSError:
                        pass

    def finish(self):
        with CONNECTIONS_LOCK:
            CONNECTIONS.pop(getattr(self, 'conn_id', None), None)
        try:
            for link_id, device in list(self.links.items()):
                logger.info('client %s dropped link %s (%s) without destroy_link',
                            self.client_address, link_id, device.device_name)
                try:
                    device.device_enable_srq(False, None)
                    device.destroy_intr_chan()
                except Exception:
                    pass
                device.lock.release(link_id)
                _device_close(device)
                self.server.link_delete(link_id)
            self.links.clear()
        finally:
            return super().finish()

    def _last_device(self):
        # create/destroy_intr_chan carry no link id: use the newest link
        return next(reversed(self.links.values()), None)

    def handle_10(self):
        '''The create_link RPC creates a new link. 
        This link is identified on subsequent RPCs by the lid returned from the network instrument server.'''
        
        params = self.unpacker.unpack_create_link_parms()
        client_id, lock_device, lock_timeout, device_name = params

        logger.debug('****************************')
        logger.debug('CREATE_LINK %s' ,params)

        link_id = 0
        error = vxi11.ERR_NO_ERROR

        try:
            logger.debug('Device name "%s"', device_name)
            link_id, self.device = self.server.link_create(device_name)
        except KeyError:
            error = vxi11.ERR_DEVICE_NOT_ACCESSIBLE
            logger.debug("Create link failed")
        else:
            self.device.client_address = self.client_address   # for monitoring
            self.device.link_id = link_id
            self.device.device_init()
            if lock_device == True:
                flags = 0
                error = self.device.lock.acquire(link_id, flags, lock_timeout)
            if error == vxi11.ERR_NO_ERROR:
                self.links[link_id] = self.device
            else:
                self.server.link_delete(link_id)   # don't leak a link the client never got
                link_id = 0

        abort_port = 0
        if error == vxi11.ERR_NO_ERROR:
            abort_port = self.server.abort_port

        result = (error, link_id, abort_port, MAX_RECEIVE_SIZE)
        self.turn_around()
        self.packer.pack_create_link_resp(result)
        return
    
    def handle_23(self):
        "The destroy_link call is used to close the identified link.  The network instrument server recovers resources associated with the link"
        
        params = self.unpacker.unpack_device_link()
        link_id = params

        if not self._select(link_id):
            error = vxi11.ERR_INVALID_LINK_IDENTIFIER
        else:
            logger.debug('DESTROY_LINK %s to %s', link_id, self.device.device_name)
            # first disable interrupt handling of the device if it was on
            self.device.device_enable_srq(False,None)
            self.device.destroy_intr_chan()

            self.device.lock.release(link_id)
            _device_close(self.device)

            # now remove link and therefore delete everything.
            self.links.pop(link_id, None)
            self.server.link_delete(link_id)
            error = vxi11.ERR_NO_ERROR
            
        self.turn_around()
        self.packer.pack_device_error(error)
        return
    
    def handle_11(self):
        "The device_write RPC is used to write data to the specified device"
        
        params = self.unpacker.unpack_device_write_parms()
        logger.debug('DEVICE_WRITE %s', params)
        link_id, io_timeout, lock_timeout, flags, opaque_data = params

        if not self._select(link_id):
            error = vxi11.ERR_INVALID_LINK_IDENTIFIER
        elif len(opaque_data) > MAX_RECEIVE_SIZE:
            error = vxi11.ERR_PARAMETER_ERROR
        else:
            with self.device.lock(link_id, flags, lock_timeout) as error:
                if error == vxi11.ERR_NO_ERROR:
                    error = self.device.device_write(opaque_data, flags, io_timeout)
                
        result = (error, 0)
        if error == vxi11.ERR_NO_ERROR:
            result = (error, len(opaque_data))

        self.turn_around()
        self.packer.pack_device_write_resp(result)
        return
    
    def handle_12(self):
        "The device_read RPC is used to read data from the device to the controller"
        
        params = self.unpacker.unpack_device_read_parms()
        logger.debug('DEVICE_READ %s', params)
        link_id, request_size, io_timeout, lock_timeout, flags, term_char = params

        opaque_data = b''
        reason = 0
        
        if not self._select(link_id):
            error = vxi11.ERR_INVALID_LINK_IDENTIFIER
        else:
            with self.device.lock(link_id, flags, lock_timeout) as error:
                if error == vxi11.ERR_NO_ERROR:
                    error, reason, opaque_data = self.device.device_read(request_size, term_char, flags, io_timeout)

        result = (error, reason, opaque_data)
        self.turn_around()
        self.packer.pack_device_read_resp(result)
        return
    
    def handle_13(self):
        "The device_readstb RPC is used to read a device's status byte."
        
        params = self.unpacker.unpack_device_generic_parms()
        logger.debug('DEVICE_READSTB %s', params)
        link_id, flags, lock_timeout, io_timeout = params

        stb=0
        if not self._select(link_id):
            error = vxi11.ERR_INVALID_LINK_IDENTIFIER
        else:
            with self.device.lock(link_id, flags, lock_timeout) as error:
                if error == vxi11.ERR_NO_ERROR:
                    error, stb = self.device.device_readstb(flags, io_timeout)
            
        result = (error, stb)
        self.turn_around()
        self.packer.pack_device_read_stb_resp(result)
        return
    
    def handle_14(self):
        "The device_trigger RPC is used to send a trigger to a device."
        
        params = self.unpacker.unpack_device_generic_parms()
        logger.debug('DEVICE_TRIGGER %s', params)
        link_id, flags, lock_timeout, io_timeout = params

        if not self._select(link_id):
            error = vxi11.ERR_INVALID_LINK_IDENTIFIER
        else:
            with self.device.lock(link_id, flags, lock_timeout) as error:
                if error == vxi11.ERR_NO_ERROR:
                    error = self.device.device_trigger(flags, io_timeout)
            
        self.turn_around()
        self.packer.pack_device_error(error)
        return
    
    def handle_15(self):
        "The device_clear RPC is used to send a device clear to a device"
        
        params = self.unpacker.unpack_device_generic_parms()
        logger.debug('DEVICE_CLEAR %s', params)
        link_id, flags, lock_timeout, io_timeout = params

        if not self._select(link_id):
            error = vxi11.ERR_INVALID_LINK_IDENTIFIER
        else:
            with self.device.lock(link_id, flags, lock_timeout) as error:
                if error == vxi11.ERR_NO_ERROR:
                    error = self.device.device_clear(flags, io_timeout)
            
        self.turn_around()
        self.packer.pack_device_error(error)
        return
    
    def handle_16(self):
        "The device_remote RPC is used to place a device in a remote state wherein all programmable local controls are disabled"
        
        params = self.unpacker.unpack_device_generic_parms()
        logger.debug('DEVICE_REMOTE %s', params)
        link_id, flags, lock_timeout, io_timeout = params

        if not self._select(link_id):
            error = vxi11.ERR_INVALID_LINK_IDENTIFIER
        else:
            with self.device.lock(link_id, flags, lock_timeout) as error:
                if error == vxi11.ERR_NO_ERROR:
                    error = self.device.device_remote(flags, io_timeout)
            
        self.turn_around()
        self.packer.pack_device_error(error)
        return
    
    def handle_17(self):
        "The device_local RPC is used to place a device in a local state wherein all programmable local controls are enabled"
        
        params = self.unpacker.unpack_device_generic_parms()
        logger.debug('DEVICE_LOCAL %s', params)
        link_id, flags, lock_timeout, io_timeout = params

        if not self._select(link_id):
            error = vxi11.ERR_INVALID_LINK_IDENTIFIER
        else:
            with self.device.lock(link_id, flags, lock_timeout) as error:
                if error == vxi11.ERR_NO_ERROR:
                    error = self.device.device_local(flags, io_timeout)
            
        self.turn_around()
        self.packer.pack_device_error(error)
        return
    
    def handle_18(self):
        "The device_lock RPC is used to acquire a device's lock"
        
        params = self.unpacker.unpack_device_lock_parms()
        logger.debug('DEVICE_LOCK %s', params)
        link_id, flags, lock_timeout = params

        if not self._select(link_id):
            error = vxi11.ERR_INVALID_LINK_IDENTIFIER
        else:
            error = self.device.lock.acquire(link_id, flags, lock_timeout)
            
        self.turn_around()
        self.packer.pack_device_error(error)
        return
    
    def handle_19(self):
        "The device_unlock RPC is used to release locks acquired by the device_lock RPC"
        
        params = self.unpacker.unpack_device_link()
        logger.debug('DEVICE_UNLOCK %s', params)
        link_id = params
 
        if not self._select(link_id):
            error = vxi11.ERR_INVALID_LINK_IDENTIFIER
        else:
            error = self.device.lock.release(link_id)

        self.turn_around()
        self.packer.pack_device_error(error)
        return
    
    def handle_25(self):
        "The create_intr_chan RPC is used to inform the network instrument server to establish an interrupt channel"
        
        params = self.unpacker.unpack_device_remote_func_parms()
        logger.debug('DEVICE_CREATE_INTR_CHAN %s', params)
        host_addr, host_port, prog_num, prog_vers, prog_family = params

        device = self._last_device()
        if device is None:
            error = vxi11.ERR_CHANNEL_NOT_ESTABLISHED
        else:
            error = device.create_intr_chan(host_addr, host_port, prog_num, prog_vers, prog_family)

        self.turn_around()
        self.packer.pack_device_error(error)
        return
    
    def handle_26(self):
        "The destroy_intr_chan RPC is used to inform the network instrument server to close its interrupt channel"
        
        # no params (void) for this function according to vxi11-spec B.6.13 V1.0 !
        logger.debug('DEVICE_DESTROY_INTR_CHAN')

        device = self._last_device()
        error = vxi11.ERR_CHANNEL_NOT_ESTABLISHED if device is None else device.destroy_intr_chan()

        self.turn_around()
        self.packer.pack_device_error(error)
        return
        
    def handle_20(self):
        "The device_enable_srq RPC is used to enable or disable the sending of device_intr_srq RPCs by thenetwork instrument server"
        
        params = self.unpacker.unpack_device_enable_srq_parms()
        logger.debug('DEVICE_ENABLE_SRQ %s', params)
        link_id, enable, handle = params

        if not self._select(link_id):
            error = vxi11.ERR_INVALID_LINK_IDENTIFIER
        else:
            error = self.device.device_enable_srq(enable,handle)

        self.turn_around()
        self.packer.pack_device_error(error)
        return
    
    def handle_22(self):
        "The device_docmd RPC allows a variety of operations to be executed"
        
        params = self.unpacker.unpack_device_docmd_parms()
        logger.debug('DEVICE_DOCMD %s', params)
        link_id, flags, io_timeout, lock_timeout, cmd, network_order, data_size, opaque_data_in = params

        opaque_data_out = b""
        if not self._select(link_id):
            error = vxi11.ERR_INVALID_LINK_IDENTIFIER
        else:
            with self.device.lock(link_id, flags, lock_timeout) as error:
                if error == vxi11.ERR_NO_ERROR:
                    error, opaque_data_out = self.device.device_docmd(flags, io_timeout, cmd, network_order, data_size, opaque_data_in)
            
        result = error, opaque_data_out
        self.turn_around()
        self.packer.pack_device_docmd_resp(result)
        return
    

class InstrumentServer():
    '''Maintains a registry of device handlers and routes incoming client RPC's to appropriate handler.
    '''
    def __init__(self, default_device_handler=None):
        '''Initialize the instrument and start a default device handler on inst0.
        
        default_device_handler: (optional) a device_handler class to be use
            as the default devive handler registered as "inst0".
        '''
        self.abortServer = Vxi11AbortServer()

        abort_host, abort_port = self.abortServer.server_address
        self.coreServer = Vxi11CoreServer(abort_port)

        if default_device_handler is None:
            default_device_handler = Instrument.DefaultInstrumentDevice

        self.add_device_handler(default_device_handler, 'inst0')
        return

    def add_device_handler(self, device_handler, device_name=None ):
        '''registers a device handler to serve client requests.

        device_handler: device handler class to handle incoming requests on device_name.
        device_name: (optional) name of device to be used in clients connect string. 
              if none supplied, next available "inst" used
        '''
        self.coreServer.device_register(device_name, device_handler)
        return(True)
    
    def close(self):
        logger.info('Closing...')
        self.coreServer.unregister()
        self.coreServer.shutdown()
        self.coreServer.server_close()

        self.abortServer.shutdown()
        self.abortServer.server_close()
        logger.info('Closed.')
        return(True)
        
    def listen(self, loglevel = 'DEBUG'): # 'INFO'
        #self.ch.setLevel(getattr(logging, loglevel))

        abortThread = threading.Thread(target=self.abortServer.serve_forever)
        abortThread.setDaemon(True) # don't hang on exit
        abortThread.start()
        logger.info('abortServer started...')

        self.coreServer.register()
        coreThread = threading.Thread(target=self.coreServer.serve_forever)
        coreThread.setDaemon(True)
        coreThread.start()
        logger.info('coreServer started...')
        return(True)
    
