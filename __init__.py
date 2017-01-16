#!/usr/bin/env python
# encoding: utf-8

import json
import libvirt
import logging
import netifaces
import os
import signal
import socket
import struct
import sys
import threading
import time
import uuid
from Queue import Queue

import sh
import slave.models
from slave.amqp_man import AmqpManager
from slave.vm import VMHandler, ImageManager
from twisted.internet import protocol, reactor, endpoints
from twisted.protocols import basic

logging.basicConfig(
    level=logging.DEBUG,
)
logging.getLogger().handlers[0].setFormatter(logging.Formatter("%(asctime)s %(levelname)s:%(name)s:%(message)s"))

logging.getLogger("sh").setLevel(logging.WARN)


def _signal_handler(signum=None, frame=None):
    """Shut down the running Master worker

    :signum: Signal number (e.g. signal.SIGINT, etc)
    :frame: Python frame (I think)
    :returns: None

    """
    logging.getLogger("Slave").info("handling signal")
    Slave.instance().stop()
    logging.shutdown()


def _install_sig_handlers():
    """Install signal handlers
    """
    print("installing signal handlers")
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)


class LibvirtWrapper(object):
    _lock = None
    _conn = None

    def __init__(self, log, uri="qemu:///system"):
        # self._lock = threading.Lock()
        self._uri = uri
        self._conn = libvirt.open(self._uri)
        self._l_log = log.getChild("LV")

        self._can_be_used = threading.Event()
        self._can_be_used.set()
        self._lock = threading.Lock()

    def restart_libvirtd(self):
        self._l_log.debug("restarting libvirtd, waiting for lock")

        self._can_be_used.clear()

        self._l_log.debug("closing connection")
        self._conn.close()
        self._l_log.info("restarting libvirtd")
        # this works better than `/etc/init.d/libvirt-bin restart` for some reason
        os.system("/etc/init.d/libvirt-bin stop")
        time.sleep(3)
        os.system("killall -KILL libvirtd")
        time.sleep(3)
        os.system("/etc/init.d/libvirt-bin start")
        self._l_log.info("sleeping for a bit until libvirt should be up")
        time.sleep(10)
        self._l_log.info("reconnecting to libvirt")
        self._conn = libvirt.open(self._uri)
        self._l_log.info("reconnected")

        self._can_be_used.set()

    def __getattr__(self, name):
        if hasattr(self._conn, name):
            val = getattr(self._conn, name)
            if hasattr(val, "__call__"):
                def wrapped(*args, **kwargs):
                    self._can_be_used.wait(2 ** 31)
                    return val(*args, **kwargs)

                return wrapped
            else:
                return val


class GuestComms(basic.LineReceiver):
    """Communicates with the guest hosts as they start running"""

    def connectionMade(self):
        self.setRawMode()

        self._unfinished_message = None

    def rawDataReceived(self, data):
        print("RECEIVED SOME DATA!!! {}".format(data))

        while len(data) > 0:
            if self._unfinished_message is not None:
                remaining = self._unfinished_message_len - len(self._unfinished_message)
                self._unfinished_message += data[0:remaining]
                data = data[remaining:]

                if len(self._unfinished_message) == self._unfinished_message_len:
                    self._handle_message(self._unfinished_message)
                    self._unfinished_message = None

            else:
                data_len = struct.unpack(">L", data[0:4])[0]
                part = data[4:4 + data_len]
                data = data[4 + data_len:]

                if len(part) < data_len:
                    self._unfinished_message_len = data_len
                    self._unfinished_message = part
                elif len(part) == data_len:
                    self._handle_message(part)

    def _handle_message(self, message_data):
        part_data = json.loads(message_data)
        res = Slave.instance().handle_guest_comms(part_data)

        if res is not None:
            self.transport.write(res)


class GuestCommsFactory(protocol.Factory):
    def buildProtocol(self, addr=None):
        return GuestComms()


class Slave(threading.Thread):
    """The slave handler"""

    AMQP_JOB_QUEUE = "jobs"
    AMQP_JOB_STATUS_QUEUE = "job_status"
    AMQP_JOB_PROPS = dict(
        durable=True,
        auto_delete=False,
        exclusive=False
    )

    AMQP_BROADCAST_XCHG = "broadcast"
    AMQP_SLAVE_QUEUE = "slaves"
    AMQP_SLAVE_STATUS_QUEUE = "slave_status"
    AMQP_SLAVE_PROPS = dict(
        durable=True,
        auto_delete=False,
        exclusive=False,
    )
    AMQP_SLAVE_STATUS_PROPS = dict(
        durable=True,
        auto_delete=False,
        exclusive=False,
    )

    _INSTANCE = None

    @classmethod
    def instance(cls, amqp_host=None, max_vms=None, intf=None):
        if cls._INSTANCE is None:
            cls._INSTANCE = cls(amqp_host, max_vms, intf)
        return cls._INSTANCE

    def __init__(self, amqp_host, max_vms, intf):
        """Init the slave"""

        super(Slave, self).__init__()

        self._max_vms = max_vms
        self._max_vms_lock = threading.Semaphore(max_vms)

        self._amqp_host = amqp_host
        self._log = logging.getLogger("Slave")

        self._running = threading.Event()
        self._slave_config_received = threading.Event()

        self._amqp_man = AmqpManager.instance(self._amqp_host)
        self._image_man = ImageManager.instance()

        self._uuid = str(uuid.uuid4())
        self._intf = intf
        self._ip = netifaces.ifaddresses(self._intf)[2][0]['addr']
        self._hostname = socket.gethostname()

        # these will be set by the config amqp message
        # see the _handle_config method
        self._db_host = None
        self._code_loc = None

        self._already_consuming = False

        self._handlers = []
        self._handlers_lock = threading.Lock()
        self._total_jobs_run = 0

        self._libvirt_conn = None

        self._gen_mac_addrs()
        self._gen_vnc_ports()

        self._libvirtd_can_be_used = threading.Event()
        # not restarting, so set it (amqp jobs will wait on it when it's cleared)
        self._libvirtd_can_be_used.set()
        # restart every thousand vms
        self._libvirtd_restart_vm_count = 1000

        self._last_vm_started_evt = threading.Event()
        self._last_vm_started_evt.set()

    def _gen_mac_addrs(self):
        self._mac_addrs = Queue()
        base = "00:00:c0:a8:7b:{:02x}"
        num = 2
        for x in xrange(50):
            new_mac = base.format(num + x)
            ip = ".".join(map(lambda x: str(int(x, 16)), new_mac.split(":")[2:]))
            # just testing this out...
            sh.arp("-s", ip, new_mac)
            self._mac_addrs.put(new_mac)

    def _gen_vnc_ports(self):
        self._vnc_ports = Queue()
        for x in xrange(50):
            self._vnc_ports.put(5900 + 5 + x)

    def run(self):
        self._running.set()
        self._log.info("running")

        self._amqp_man.declare_exchange(self.AMQP_BROADCAST_XCHG, "fanout")

        self._amqp_man.declare_queue(self.AMQP_JOB_QUEUE, **self.AMQP_JOB_PROPS)
        self._amqp_man.declare_queue(self.AMQP_JOB_STATUS_QUEUE, **self.AMQP_JOB_PROPS)

        self._amqp_man.declare_queue(self.AMQP_SLAVE_QUEUE, **self.AMQP_SLAVE_PROPS)
        self._amqp_man.declare_queue(self.AMQP_SLAVE_STATUS_QUEUE, **self.AMQP_SLAVE_STATUS_PROPS)
        self._amqp_man.declare_queue(
            self.AMQP_SLAVE_QUEUE + "_" + self._uuid,
            exclusive=True
        )
        self._amqp_man.bind_queue(
            exchange=self.AMQP_BROADCAST_XCHG,
            queue=self.AMQP_SLAVE_QUEUE + "_" + self._uuid
        )
        self._amqp_man.do_start()
        self._amqp_man.wait_for_ready()

        self._amqp_man.queue_msg(
            json.dumps(dict(
                type="new",
                uuid=self._uuid,
                ip=self._ip,
                hostname=self._hostname,
                max_vms=self._max_vms,
            )),
            self.AMQP_SLAVE_STATUS_QUEUE
        )

        self._amqp_man.consume_queue(self.AMQP_SLAVE_QUEUE, self._on_slave_all_received)
        self._amqp_man.consume_queue(self.AMQP_SLAVE_QUEUE + "_" + self._uuid, self._on_slave_me_received)

        self._log.info("waiting for slave config to be received")

        self._status_update_thread = threading.Thread(target=self._do_update_status)
        self._status_update_thread.daemon = True
        self._status_update_thread.start()

        while self._running.is_set():
            time.sleep(0.2)

        self._log.info("finished")

    def stop(self):
        """
        Stop the slave
        """
        self._log.info("stopping!")
        # logging module does not work from within a signal handler (which
        # is where stop may be called from). See
        # https://docs.python.org/2/library/logging.html#thread-safety
        print("stopping!")

        self._amqp_man.stop()
        self._running.clear()

        for handler in self._handlers:
            handler.stop()
            # wait for them all to finish!
            handler.join()

        self._log.info("all handlers have exited, goodbye")
        print("all handlers have exited, goodbye")

        logging.shutdown()

    def cancel_job(self, job):
        """
        Cancel the job with job id ``job``

        :job: The job id to cancel
        """
        for handler in self._handlers:
            if handler.job == job:
                self._log.debug("cancelling handler for job {}".format(job))
                handler.stop()
        self._log.warn("could not find handler for job {} to cancel".format(job))

    # -----------------------
    # guest comms
    # -----------------------

    def handle_guest_comms(self, data):
        self._log.info("received guest comms! {}".format(str(data)[:100]))

        if "type" not in data:
            self._log.warn("type not found in guest comms data: {}".format(data))
            return "{}"

        switch = dict(
            started=self._handle_job_started,
            progress=self._handle_job_progress,
            result=self._handle_job_result,
            finished=self._handle_job_finished,
            error=self._handle_job_error,
            logs=self._handle_job_logs,
        )

        if data["type"] not in switch:
            self._log.warn("unhandled guest comms type: {}".format(data["type"]))
            return

        return switch[data["type"]](data)

    def _handle_job_started(self, data):
        self._log.debug("handling started job part: {}:{}".format(data["job"], data["idx"]))

        found_handler = None
        with self._handlers_lock:
            for handler in self._handlers:
                if handler.job == data["job"] and handler.idx == data["idx"]:
                    found_handler = handler

        if found_handler is not None:
            found_handler.on_received_started()
            self._last_vm_started_evt.set()
        else:
            self._log.warn("cannot find the handler for data: {}".format(data))

    def _handle_job_error(self, data):
        self._log.debug("handling job errors: {}:{}".format(data["job"], data["idx"]))

        self._amqp_man.queue_msg(
            json.dumps(dict(
                type="error",
                tool=data["tool"],
                idx=data["idx"],
                job=data["job"],
                data=data["data"]
            )),
            self.AMQP_JOB_STATUS_QUEUE
        )

    def _handle_job_logs(self, data):
        self._log.debug("handling debug logs from job part: {}:{}".format(data["job"], data["idx"]))

        self._amqp_man.queue_msg(
            json.dumps(dict(
                type="log",
                tool=data["tool"],
                idx=data["idx"],
                job=data["job"],
                data=data["data"]
            )),
            self.AMQP_JOB_STATUS_QUEUE
        )

    def _handle_job_finished(self, data):
        self._log.debug("handling finished job part: {}:{}".format(data["job"], data["idx"]))

        found_handler = None
        with self._handlers_lock:
            for handler in self._handlers:
                if handler.job == data["job"] and handler.idx == data["idx"]:
                    found_handler = handler

        if found_handler is not None:
            found_handler.stop()
        else:
            self._log.warn("cannot find the handler for data: {}".format(data))

    def _handle_job_progress(self, data):
        self._log.debug("handling job progress: {}:{}".format(data["job"], data["idx"]))

        matched_handler = None
        for handler in self._handlers:
            if handler.job == data["job"] and handler.idx == data["idx"]:
                matched_handler = handler
                break

        if matched_handler is not None:
            matched_handler.total_progress += data["data"]

        self._amqp_man.queue_msg(
            json.dumps(dict(
                type="progress",
                job=data["job"],
                idx=data["idx"],
                amt=data["data"],  # it's expected to just be a number
            )),
            self.AMQP_JOB_STATUS_QUEUE
        )

    def _handle_job_result(self, data):
        self._log.debug("handling job result: {}:{}".format(data["job"], data["idx"]))

        self._amqp_man.queue_msg(
            json.dumps(dict(
                type="result",
                tool=data["tool"],
                idx=data["idx"],
                job=data["job"],
                data=data["data"],
                slave=self._hostname,
            )),
            self.AMQP_JOB_STATUS_QUEUE
        )

    # -----------------------
    # amqp stuff
    # -----------------------

    def _on_job_received(self, channel=None, method, properties=None, body):
        """
        """
        self._max_vms_lock.acquire()

        # wait for any restarts to be
        if not self._libvirtd_can_be_used.is_set():
            self._log.debug("waiting for libvirtd to restart before starting any new VMs")
        self._libvirtd_can_be_used.wait(2 ** 31)

        self._log.info("received job from queue: {}".format(body))

        self._amqp_man.ack_method(method)
        data = json.loads(body)

        jobs = slave.models.Job.objects(id=data["job"])
        if len(jobs) == 0:
            self._log.warn("received a job that doesn't exist???")
            self._max_vms_lock.release()
            return

        job_obj = jobs[0]
        if job_obj.status["name"] != "running":
            self._log.warn("job's state is not 'running', so not running it (was {})".format(job_obj.status["name"]))
            self._max_vms_lock.release()
            return

        # wait until the last vm started to start the next vm
        self._log.debug("waiting for last vm to start running tool")
        self._last_vm_started_evt.wait(2 ** 31)
        self._log.debug("last vm started! running next vm")
        self._last_vm_started_evt.clear()

        try:
            handler = VMHandler(
                job=data["job"],
                idx=data["idx"],
                debug=data["debug"],
                image=data["image"],
                image_username=data["image_username"],
                image_password=data["image_password"],
                os_type=data["os_type"],
                tool=data["tool"],
                params=data["params"],
                network=data["network"],
                fileset=data["fileset"],
                timeout=data["vm_max"],
                db_host=self._db_host,
                code_loc=self._code_loc,
                code_username=self._code_username,
                code_password=self._code_password,
                on_finished=self._on_vm_handler_finished,
                libvirt_conn=self._get_libvirt_conn(),
                mac=self._get_next_mac(),
                vnc_port=self._get_next_vnc(),
            )
        except KeyError:
            self._log.warn("received malformed job: {!r}".format(data))
            self._max_vms_lock.release()
            return
        else:
            with self._handlers_lock:
                self._handlers.append(handler)
            handler.start()

        self._update_status()
        self._log.debug("done starting VMHandler")

    def _get_next_vnc(self):
        return self._vnc_ports.get()

    def _get_next_mac(self):
        return self._mac_addrs.get()

    def _get_libvirt_conn(self):
        if self._libvirt_conn is None:
            self._libvirt_conn = LibvirtWrapper(self._log)
        # self._libvirt_conn = libvirt.open("qemu:///system")

        return self._libvirt_conn

    def _on_vm_handler_finished(self, handler):
        """
        Handle a finished VM handler
        """
        self._log.debug("The VM handler {} for job {}:{} has finished".format(handler, handler.job, handler.idx))

        # it never reported progress for some reason, so let's report a progress of 1
        # for just having run the VM (1 vm == 1 progress. ALWAYS!)
        if handler.total_progress == 0 and handler._received_started_msg:
            self._log.info("job handler exited without reporting any progress, reporting 1 progress")
            self._handle_job_progress({
                "job": handler.job,
                "idx": handler.idx,
                "data": 1
            })

        # vm died, never received started message, so don't block anymore
        if not handler._received_started_msg:
            self._last_vm_started_evt.set()

        with self._handlers_lock:
            self._handlers.remove(handler)
            self._total_jobs_run += 1

        #			if self._total_jobs_run % self._libvirtd_restart_vm_count == 0:
        #				self._log.debug("clearing libvirt restart event")
        #				self._libvirtd_can_be_used.clear()
        #
        #			if len(self._handlers) == 0 and not self._libvirtd_can_be_used.is_set():
        #				self._log.debug("no vms are running and libvirtd needs to be restarted, so restarting it")
        #				self._libvirt_conn.restart_libvirtd()
        #				self._log.debug("setting libvirtd_can_be_used event")
        #				self._libvirtd_can_be_used.set()

        self._mac_addrs.put(handler.mac)
        self._vnc_ports.put(handler.vnc_port)
        self._max_vms_lock.release()
        self._update_status()

    def _on_vm_handler_vnc_avail(self, handler=None):
        """
        Send a new status update that will include the changes in the vnc
        info in the handler.
        """
        self._update_status()

    def _on_slave_all_received(self, channel=None, method, properties=None, body):
        """
        """
        self._log.info("received slave all msg from queue: {}".format(body))

        data = json.loads(body)

        if "type" not in data:
            self._log.warn("all slaves type specifier was not in data: {}".format(data))
            return

        self._amqp_man.ack_method(method)

    def _on_slave_me_received(self, channel=None, method, properties=None, body):
        """
        """
        self._log.info("received slave me msg from queue: {}".format(body))
        self._amqp_man.ack_method(method)

        data = json.loads(body)

        switch = dict(
            config=self._handle_config,
            cancel=self._handle_job_cancel,
        )

        if "type" not in data or data["type"] not in switch:
            self._log.debug("malformed data received on me slave queue: {}".format(body))
        else:
            switch[data["type"]](data)

    def _handle_job_cancel(self, data):
        self._log.info("handling a job cancellation: {}".format(data))

        if "job" not in data:
            self._log.warn("the job was not specified")
            return

        self.cancel_job(data["job"])

    def _handle_config(self, data):
        self._log.info("handling config: {}".format(data))

        if "db" in data:
            self._log.info("connecting to mongodb at {}".format(data["db"]))
            self._db_host = data["db"]
            slave.models.do_connect(self._db_host)

        if "code" in data:
            self._log.info("setting code loc to {}".format(data["code"]["loc"]))
            self._code_loc = data["code"]["loc"]
            self._code_username = data["code"]["username"]
            self._code_password = data["code"]["password"]

        if "image_url" in data:
            self._log.info("setting image url to {}".format(data["image_url"]))
            self._image_url = data["image_url"]
            self._image_man.instance().image_url = self._image_url

        if not self._already_consuming:
            self._amqp_man.consume_queue(self.AMQP_JOB_QUEUE, self._on_job_received)
            self._already_consuming = True

    # -----------------------

    def _do_update_status(self):
        while self._running.is_set():
            self._update_status()
            time.sleep(5)

    def _update_status(self):
        vm_infos = []
        for handler in self._handlers:
            vm_infos.append(dict(
                job=handler.job,
                idx=handler.idx,
                vnc_port=handler.vnc_port,
                tool=handler.tool,
                start_time=handler.start_time,
                vm_status=handler.vm_status,
            ))

        self._amqp_man.queue_msg(
            json.dumps(dict(
                type="status",
                uuid=self._uuid,
                running_vms=len(self._handlers),
                total_jobs_run=self._total_jobs_run,
                vms=vm_infos
            )),
            self.AMQP_SLAVE_STATUS_QUEUE
        )


def main(amqp_host, max_vms, intf):
    # _install_sig_handlers()

    virt_ip = netifaces.ifaddresses('virbr2')[2][0]['addr']
    endpoints.serverFromString(reactor, "tcp:55555:interface={}".format(virt_ip)).listen(GuestCommsFactory())

    slave = Slave.instance(amqp_host, max_vms, intf)
    reactor.callWhenRunning(slave.start)
    reactor.addSystemEventTrigger("during", "shutdown", Slave.instance().stop)
    reactor.run()


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.stderr.write("USAGE: {} <AMQP_HOST> <MAX_VMS>\n")
        exit(1)

    main(sys.argv[1], sys.argv[2])
