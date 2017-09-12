#!/usr/bin/env python
# encoding: utf-8



"""
This is the bootstrap that installs required code at runtime
and runs the tool for the job.

The bootstrap is also responsible for communicating with the
slave daemon on the host (this is assumed to be running in
a virtual machine guest).
"""


from __future__ import absolute_import


from pprint import pprint
from requests.auth import HTTPBasicAuth
import base64
import imp
import json
import logging
import marshal
import os
import pip
import pip.req
import re
import requests
import select
import shutil
import site
import socket
import struct
import subprocess
import sys
import tarfile
import threading
import time
import traceback


class FriendlyJSONEncoder(json.JSONEncoder):
    """Encode json data correctly. Created in order to handle
    ObjectId instances correctly.
    """
    def default(self, o):
        # I don't want to have to import bson here, since that's installed
        # via a top-level requirements.txt with pymongo. We'll just check the
        # class name instead (HACK).
        # 
        # also see the note in HostComms.send_msg
        if o.__class__.__name__ == "ObjectId":
            return str(o)
        else:
            return super(self.__class__, self).default(o)


ORIG_ARGS = sys.argv

# clear out any existing handlers
logging.getLogger().handlers = []

logging.getLogger("urllib3").setLevel(logging.WARN)
logging.getLogger("pip").setLevel(logging.WARN)

DEV = len(sys.argv) > 1 and sys.argv[1] == "dev"

log_file = os.path.join(os.path.dirname(__file__), __file__.split(".")[0] + ".log")
if DEV:
    logging.basicConfig(level=logging.DEBUG)
else:
    logging.basicConfig(filename=log_file, level=logging.DEBUG)
logging.getLogger("requests").setLevel(logging.CRITICAL)

stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(name)s:%(levelname)s: %(message)s')
stream_handler.setFormatter(formatter)
logging.getLogger().addHandler(stream_handler)


class LogAccumulator(logging.Handler):
    def __init__(self):
        super(self.__class__, self).__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def get_records(self):
        formatter = logging.Formatter('%(asctime)s %(levelname)s:%(name)s:%(message)s')
        res = []
        for record in self.records:
            res.append(formatter.format(record))
        return res


class HostComms(threading.Thread):
    """Communications class to communicate with the host.
    """
    def __init__(self, recv_callback, job_id, job_idx, tool, dev=False):
        """Create the HostComms instance.

        :param function recv_callback: A callback to call when new messages are received.
        :param str job_id: The id of the current job.
        :param int job_idx: The index into the current job.
        :param str tool: The name of the tool to run.
        :param bool dev: If this class should run in dev mode.
        """
        super(HostComms, self).__init__()

        self._log = logging.getLogger("HostComms")
        self._dev = dev

        # spin until we have a real ip address
        while True:
            self._my_ip = socket.gethostbyname(socket.gethostname())
            if self._my_ip.startswith("127.0"):
                p = subprocess.Popen(["ifconfig", "eth0"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                stdout, stderr = p.communicate()
                for line in stdout.split("\n"):
                    if "inet addr:" in line:
                        match = re.match(r'^\s*inet addr:(\d+\.\d+\.\d+\.\d+).*$', line)
                        self._my_ip = match.group(1)
                        break

            if not self._my_ip.startswith("192.168.123."):
                self._log.debug("We don't have an ip address yet (currently '{}')".format(self._my_ip))
                time.sleep(0.2)
                continue

            break

        self._log.debug("We have an ip! {}".format(self._my_ip))

        self._host_ip = self._my_ip.rsplit(".", 1)[0] + ".1"
        self._host_port = 55555

        self._job_id = job_id
        self._job_idx = job_idx
        self._tool = tool

        self._running = threading.Event()
        self._running.clear()
        self._connected = threading.Event()
        self._connected.clear()

        self._send_recv_lock = threading.Lock()

        self._recv_callback = recv_callback

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    def run(self):
        """Run the host comms in a new thread.

        :returns: None
        """
        self._log.info("Running")
        self._running.set()

        if not self._dev:
            self._sock.connect((self._host_ip, self._host_port))

        self._connected.set()

        # select on the socket until we're told not to run anymore
        while self._running.is_set():
            if not self._dev:
                reads, _, _ = select.select([self._sock], [], [], 0.1)
                if len(reads) > 0:
                    data = ""
                    with self._send_recv_lock:
                        while True:
                            recvd = self._sock.recv(0x1000)
                            if len(recvd) == 0:
                                break
                            data += recvd
                    self._recv_callback(data)
            time.sleep(0.1)

        self._log.info("Finished")

    def stop(self):
        """Stop the host comms from running
        """
        self._log.info("Stopping")
        self._running.clear()

    def send_msg(self, type, data):
        """Send a message to the host.

        :param str type: The type of message to send
        :param dict data: The data to send to the host
        :returns: None
        """
        data = json.dumps(
            {
                "job": self._job_id,
                "idx": self._job_idx,
                "tool": self._tool,
                "type": type,
                "data": data
            },

            # use this so that users don't run into errors with ObjectIds not being
            # able to be encodable. If using bson.json_util.dumps was strictly used
            # everywhere, could just use that dumps method, but it's not, and I'd rather
            # keep it simple for now
            cls=FriendlyJSONEncoder
        )

        self._connected.wait(2 ** 31)

        data_len = struct.pack(">L", len(data))
        if not self._dev:
            try:
                with self._send_recv_lock:
                    self._sock.send(data_len + data)
            except:
                # yes, just silently fail I think???
                pass


class TalusCodeImporter(object):
    """This class will dynamically import tools and components from the
    talus git repository.

    This class *should* conform to "pep-302":https://www.python.org/dev/peps/pep-0302/
    """

    def __init__(self, loc, username, password, parent_log=None):
        """Create a new talus code importer that will fetch code from the specified
        ``location``, using ``username`` and ``password``.

        :param str loc: The repo location (e.g. ``https://....`` or ``ssh://...``)
        :param str username: The username to fetch the code with
        :param str password: The password to fetch the code with
        """
        if parent_log is None:
            parent_log = logging.getLogger("BOOT")
        self._log = parent_log.getChild("importer")

        self.loc = loc
        self.pypi_loc = loc.replace("/code_cache", "/pypi/")
        if self.loc.endswith("/"):
            self.loc = self.loc[:-1]

        self.username = username
        self.password = password

        self._code_dir = os.path.join(os.path.abspath(os.path.dirname(__file__)), "TALUS_CODE")
        if not os.path.exists(self._code_dir):
            os.makedirs(self._code_dir)
        sys.path.insert(0, self._code_dir)

        self._pypi_dir = os.path.join(os.path.abspath(os.path.dirname(__file__)), "TALUS_PYPI")
        if not os.path.exists(self._pypi_dir):
            os.makedirs(self._pypi_dir)
            os.makedirs(os.path.join(self._pypi_dir, "simple"))

        # since we will be installing code via "pip install --user ...", make
        # sure the user site is on our path
        if not os.path.exists(site.USER_SITE):
            os.makedirs(site.USER_SITE)
        sys.path.insert(0, site.USER_SITE)

        self.cache = {}

        dir_check = lambda x: x.endswith("/")

        self.cache["git"] = {"__items__": set(["talus/"]), "talus/": {"__items__": set()}}

        # this will add lib to the cache
        # these are python packages that are stored in git (and aren't cachable in our
        # own pypi)
        package_items = self._git_show("talus/packages")["items"]
        self.cache["packages"] = dict(map(lambda x: (x.replace("/", ""), True), filter(dir_check, package_items)))

        pypi_items = self._git_show("talus/pypi/simple")["items"]
        self.cache["pypi"] = dict(map(lambda x: (x.replace("/", ""), True), filter(dir_check, pypi_items)))

    def find_module(self, abs_name, path=None):
        """Normally, a finder object would return a loader that can load the module.
        In our case, we're going to be sneaky and just download the files and return
        ``None`` and let the normal sys.path-type loading take place.

        This method is cleaner and less error prone

        :param str abs_name: The absolute name of the module to be imported
        """
        package_name = abs_name.split(".")[0]

        last_name = abs_name.split(".")[-1]
        if last_name in sys.modules:
            return None

        try:
            # means it can already be imported, no work to be done here
            imp.find_module(abs_name)

            # THIS IS IMPORTANT, YES WE WANT TO RETURN NONE!!!
            # THIS IS IMPORTANT, YES WE WANT TO RETURN NONE!!!
            # THIS IS IMPORTANT, YES WE WANT TO RETURN NONE!!!
            # THIS IS IMPORTANT, YES WE WANT TO RETURN NONE!!!
            # see the comment in the docstring
            return None
        except ImportError as e:
            pass

        if package_name == "talus" and self._module_in_git(abs_name):
            self.download_module(abs_name)
            # THIS IS IMPORTANT, YES WE WANT TO RETURN NONE!!!
            # THIS IS IMPORTANT, YES WE WANT TO RETURN NONE!!!
            # THIS IS IMPORTANT, YES WE WANT TO RETURN NONE!!!
            # THIS IS IMPORTANT, YES WE WANT TO RETURN NONE!!!
            # see the comment in the docstring
            return None

        if package_name in self.cache["packages"] and package_name not in sys.modules:
            self.install_package_from_talus(package_name)
            return None

        # we NEED to have the 2nd check here or else it will keep downloading
        # the same package over and over
        if package_name in self.cache["pypi"] and package_name not in sys.modules:
            self.install_cached_package(package_name)
            return None

        # THIS IS IMPORTANT, YES WE WANT TO RETURN NONE!!!
        # THIS IS IMPORTANT, YES WE WANT TO RETURN NONE!!!
        # THIS IS IMPORTANT, YES WE WANT TO RETURN NONE!!!
        # THIS IS IMPORTANT, YES WE WANT TO RETURN NONE!!!
        # see the comment in the docstring
        return None
    
    def download_module(self, abs_name):
        """Download the module found at ``abs_name`` from the talus git repository

        :param str abs_name: The absolute module name of the module to be downloaded
        """
        self._log.info("Downloading module {}".format(abs_name))

        path = abs_name.replace(".", "/")
        filepath = path + ".py"

        info = self._git_show(path)

        if info is None:
            info_test = self._git_show(path + ".py")
            if info_test is None:
                raise ImportError(abs_name)
            info = info_test

        self._download(path, info=info)

        return None

    def install_cached_package(self, package_name):
        """Install the python package from our cached pypi.

        :param str package_name: The name of the python package to install
        :returns: None
        """
        self._log.info("Installing package {!r} from talus pypi".format(package_name))
        pinfo = self.cache["pypi"][package_name]
        pypi_hostname = re.match(r'^.*://([^/]+)/.*$', self.pypi_loc).group(1)

        try:
            self._run_pip_main([
                "install",
                "--user",
                "--trusted-host", pypi_hostname,
                "-i", self.pypi_loc,
                package_name
            ])
        except SystemExit as e:
            raise Exception("Is SystemExit expected?")

    def install_package_from_talus(self, package_name):
        """Install the package in talus/packages
        """
        self._log.info(
            "Installing package {!r} from talus_code/talus/packages".format(package_name)
        )

        info = self._git_show("talus/packages/{}".format(package_name))

        # should handle .tar.gz files if available
        self._download("talus/packages/{}".format(package_name))

        full_path = os.path.join(
            self._code_dir,
             "talus",
             "packages",
             package_name
        )

        pypi_hostname = re.match(r'^.*://([^/]+)/.*$', self.pypi_loc).group(1)
        try:
            self._run_pip_main([
                "install",
                "--user",
                "--upgrade",
                # point pip at the talus pypi in case the package has a
                # requirements.txt
                "--trusted-host", pypi_hostname,
                "-i", self.pypi_loc,
                full_path
            ])
        except SystemExit as e:
            # TODO
            raise Exception("Is SystemExit normal?")

    def _download_file(self, path, info=None):
        """Download the specified resource at ``path``, optionally using the
        already-fetched ``info`` (result of ``self._git_show(path)``).

        :param str path: The relative path using '/' separators inside the talus_code repository
        :param dict info: The result of ``self._git_show(path)`` (default=``None``)
        :returns: None
        """
        self._log.debug("Downloading file {!r}".format(path))

        if info is None:
            info = self._git_show(path)

        # info *SHOULD* be a basestring
        if not isinstance(info, basestring):
            raise Exception("{!r} was not a file! (info was {!r})".format(
                path,
                info
            ))

        dest_path = os.path.join(self._code_dir, path.replace("/", os.path.sep))
        self._save_file(dest_path, info)

    def _save_file(self, file_path, data):
        """Save the single file from git
        
        :param str file_path: The path to save the data to
        :param str data: The data for the file at ``file_path``
        :returns: None
        """
        self._ensure_directory(os.path.dirname(file_path))
        with open(file_path, "wb") as f:
            f.write(data)

    def _ensure_directory(self, dirname):
        """Make sure the directory exists.

        :param str dirname: The directory to ensure
        :returns: None
        """
        if not os.path.exists(dirname):
            os.makedirs(dirname)

    def _download_folder(self, path, info=None, install_requirements=True):
        """Download the folder specified by ``path``, possibly using already-fetched
        ``info`` (result of ``self._git_show(path)``), optionally installing the
        requirements.txt that may exist inside of the downloaded folder.

        If the specified path is in one of the main subdirectories (see ``_is_in_main_subdir``),
        the .tar.gz of the directory will be downloaded and extracted, if it exists.

        :param str path: The relative path in the talus_code repository. Uses '/' for separators
        :param dict info: Result of ``self._git_show(path)`` (default=``None``)
        :param bool install_requirements: Whether requirements.txt should be installed (if it exists).
            (default=True)
        """
        self._log.debug("Downloading directory {!r}".format(path))
        if info is None:
            info = self._git_show(path)

        # check for compressed version of the folder
        tar_gz_name = os.path.basename(path) + ".tar.gz"
        parent_info = self._git_show(os.path.dirname(path))
        if parent_info is None:
            parent_info = {"items":[]}
        if self._is_in_main_subdir(path) and tar_gz_name in parent_info["items"]:
            self._log.debug("Found compressed version of main subdir {!r}".format(
                path
            ))
            self._download_compressed_dir(path + ".tar.gz")

        # download like normal
        else:
            # only recursively download tools/components/packages/lib folders/subdirectories
            recurse = self._is_in_main_subdir(path, descendant=True)
            for item in info["items"]:
                if item.endswith("/") and not recurse:
                    continue

                item_path = path + "/" + item

                # don't just download all of the .tar.gz's in main subdirectories!
                if item_path.endswith(".tar.gz") and self._is_in_main_subdir(item_path):
                    continue

                self._download(item_path)

        # install requirements.txt if requested
        if install_requirements and "requirements.txt" in info["items"]:
            self.install_requirements(path + "/requirements.txt")
    
    def install_requirements(self, rel_path):
        """Install the requirements.txt located at ``rel_path``. Note that
        the path is a relative path using ``/`` separators.

        :param str rel_path: The relative path inside of ``self._code_dir``
        :returns: None
        """
        self._log.debug("Installing requirements {}".format(rel_path))

        rel_path = rel_path.replace("/", os.path.sep)
        full_path = os.path.join(self._code_dir, rel_path)

        with open(full_path, "rb") as f:
            data = f.read()

        # this takes a fair amount of time sometimes, so if there's an
        # empty requirements.txt file, skip installing it
        actual_req_count = 0
        for line in data.split("\n"):
            line = line.strip()
            if line == "" or line.startswith("#"):
                continue
            actual_req_count += 1
        if actual_req_count == 0:
            self._log.debug("Empty requirements.txt, skipping")
            return

        try:
            threading.local().indentation = 0
            pypi_hostname = re.match(r'^.*://([^/]+)/.*$', self.pypi_loc).group(1)
            self._run_pip_main([
                "install",
                    "--user",
                    "--trusted-host", pypi_hostname,
                    "-i", self.pypi_loc,
                    "-r", full_path
            ])
        
        # this is expected - pip.main will *always* exit
        except SystemExit as e:
            # TODO
            raise Exception("Is SystemExit normal?")

        threading.local().indentation = 0

    def _is_in_main_subdir(self, path, descendant=False):
        """Return ``True`` or ``False`` if the specified path is in one of the
        main subdirectories of the talus codebase. I.e. if it is a subfolder of
        or a file in one the following directories:

          * talus/tools
          * talus/components
          * talus/packages
          * talus/lib

        :param str path: The path to check
        :param bool descendant: If the path can be a several levels deep inside
            a main subdirectory.
        :returns: bool
        """
        base_regex = r'^(talus\/(tools|packages|components|lib))/'

        # anything inside that directory
        if descendant:
            regex = base_regex + ".*"

        # strictly must be an immediate child of one of the
        # main subdirectories
        else:
            regex = base_regex + r'[^/]*$'

        main_subdir_match = re.match(regex, path)
        return main_subdir_match is not None

    def _download(self, path, info=None):
        """Download the specified ``path``, possibly using an already-
        fetched ``info`` (result of ``self._git_show(path)``), optionally
        recursively downloading the folder.

        :param str path: The relative path in the talus_code repository. Uses '/' for separators
        :param dict info: Result of ``self._git_show(path)`` (default=``None``)
        :returns: None
        """
        if info is None:
            info = self._git_show(path)

        if info is None:
            raise Exception("Error! could not get information from code cache about {!r}".format(path))

        # it's a directory that we're downloading
        if isinstance(info, dict):
            self._download_folder(path, info=info)

        # it's a single file that we're downloading
        elif isinstance(info, basestring):
            self._download_file(path, info=info)

        else:
            raise Exception("Unknown resource info: {!r}".format(info))

    def _download_compressed_dir(self, tar_gz_path):
        """Download/make available/install the compressed directory. This also includes
        installing the requirements.txt if it exists.

        :param str dest: The path to where the tar file (and extracted contents) should be saved
        :param str tar_gz_name: The path to download from code cache
        :returns: None
        """
        self._log.debug("Downloading compressed directory at {!r}".format(tar_gz_path))

        tar_gz = self._git_show(tar_gz_path)
        dest_tar_path = os.path.join(self._code_dir, tar_gz_path.replace("/", os.path.sep))
        self._ensure_directory(os.path.dirname(dest_tar_path))

        self._save_file(dest_tar_path, tar_gz)
        self._extract(dest_tar_path)

    def _extract(self, file_path):
        """
        Extracts the .tar.gz file at the specified abs_name

        :param file_path: path to file
        :return: None
        """
        self._log.debug("Extracting file {!r}".format(
            os.path.basename(file_path)
        ))
        tar = tarfile.open(file_path, "r:gz")
        tar.extractall(os.path.dirname(file_path))
        tar.close()

    def _run_pip_main(self, pip_args):
        proc = subprocess.Popen(["pip"] + pip_args)
        proc.communicate()
    
    def _git_show(self, path, ref="HEAD"):
        """Return the json object returned from the /code_cache on the web server

        :str param path: The talus-code-relative path to get information about (file or directory)
        :str param ref: The reference with which to lookup the code (can be a branch, commit, etc)
        """
        res = requests.get(
            "/".join([self.loc, ref, path]),
            auth=HTTPBasicAuth(self.username, self.password)
        )

        if res.status_code // 100 != 2:
            return None

        if res.headers['Content-Type'] == 'application/json':
            res = json.loads(res.content)
            # cache existence info about all directories shown!
            if path != "talus/pypi/simple" and res["type"] == "listing":
                self._add_to_cache(path, items=res["items"])
        else:
            res = res.content

        return res

    def _add_to_cache(self, path, items=None):
        cache = root = self.cache["git"]

        parts = path.split("/")
        for part in parts:
            cache = cache.setdefault(part + "/", {"__items__": set()})

        for item in items:
            cache["__items__"].add(item)

    def _module_in_git(self, modname):
        parts = modname.split(".")

        cache = self.cache["git"]

        for part in parts[:-1]:
            items = cache["__items__"]
            if part + "/" not in items:
                return False

            cache = cache[part + "/"]

        last_part = parts[-1]
        items = cache["__items__"]

        # e.g. talus.fileset
        if last_part + ".py" in items:
            return True

        # e.g. talus.tools
        if last_part + "/" in items:
            return True

        return False


class TalusBootstrap(object):
    """The main class that will bootstrap the job and get things running
    """

    def __init__(self, config_path, dev=False):
        """
        :param str config_path: The path to the config file containing json information about the job
        """
        self._log = logging.getLogger("BOOT")
        self._log_accumulator = LogAccumulator()
        # add this to the root logger so it will capture EVERYTHING
        logging.getLogger().addHandler(self._log_accumulator)

        if not os.path.exists(config_path):
            self._log.error("ERROR, config path {} not found!".format(config_path))
            self._flush_logs()
            exit(1)

        with open(config_path, "r") as f:
            self._config = json.loads(f.read())

        self._job_id = self._config["id"]
        self._idx = self._config["idx"]
        self._tool = self._config["tool"]
        self._params = self._config["params"]
        self._fileset = self._config["fileset"]
        self._db_host = self._config["db_host"]
        self._debug = self._config["debug"]
        self._num_progresses = 0

        self.dev = dev
        self._host_comms = HostComms(self._on_host_msg_received, self._job_id, self._idx, self._tool, dev=dev)

    def _flush_logs(self):
        logging.shutdown()
    
    def sys_except_hook(self, type_, value, traceback):
        formatted = traceback.format_exception(type_, value, traceback)

        self._log.exception("There was an exception!")
        self._log.error(formatted)
        cleanup()
        try:
            self._host_comms.send_msg("error", {
                "message": str(value),
                "backtrace": formatted,
                "logs": self._log_accumulator.get_records()
            })
        except:
            pass

    def run(self):
        self._log.info("Running bootstrap")
        start_time = time.time()

        self._host_comms.start()
        self._host_comms.send_msg("installing", {})

        self._install_code_importer()

        talus_mod = __import__("talus", globals(), locals(), fromlist=["job"])

        fileset_mod = getattr(talus_mod, "fileset")
        fileset_mod.set_connection(self._db_host)

        job_mod = getattr(talus_mod, "job")
        Job = getattr(job_mod, "Job")
        try:
            job = Job(
                id=self._job_id,
                idx=self._idx,
                tool=self._tool,
                params=self._params,
                fileset_id=self._fileset,
                progress_callback=self._on_progress,
                results_callback=self._on_result,
            )

            self._log.info("Took {:.02f}s to start running job!".format(
                time.time() - start_time
            ))
            # should be a signal that things are about to start running
            self._host_comms.send_msg("started", {})
            job.run()
        except Exception as e:
            self._log.exception("Job had an error!")
            formatted = traceback.format_exc()
            self._host_comms.send_msg("error", {
                "message": str(e),
                "backtrace": formatted,
                "logs": self._log_accumulator.get_records()
            })
        self._flush_logs()

        # if the debug flag was set, then ALWAYS store the logs!
        if self._debug:
            self._host_comms.send_msg("logs", {
                "message": "DEBUG LOGS",
                "backtrace": "",
                "logs": self._log_accumulator.get_records()
            })


        if self._num_progresses == 0:
            self._log.info("Progress was never called, but job finished running, inc progress by 1")
            self._on_progress(1)

        self._host_comms.send_msg("finished", {})
        self._host_comms.stop()

        self._shutdown()

    def _shutdown(self):
        """shutdown the vm"""
        os_name = os.name.lower()
        if os_name == "nt":
            os.system("shutdown /t 0 /s /f")
        else:
            os.system("shutdown -h now")

    def _on_host_msg_received(self, data):
        """Handle the data received from the host

        :param str data: The raw data (probably in json format)
        """
        self._log.info("Received a message from the host: {}".format(data))
        data = json.loads(data)

    def _on_progress(self, num):
        """Increment the progress count for this job by ``num``

        :param int num: The number to increment the progress count of this job by
        """
        self._num_progresses += num
        self._log.info("Progress incrementing by {}".format(num))
        self._host_comms.send_msg("progress", num)

    def _on_result(self, result_type, result_data):
        """Append this to the results for this job

        :param object result_data: Any python object to be stored with this job's results (str, dict, a number, etc)
        """
        self._log.info("Sending result")
        self._host_comms.send_msg("result", {"type": result_type, "data": result_data})

    def _install_code_importer(self):
        """Install the sys.meta_path finder/loader to automatically load modules from
        the talus git repo.
        """
        self._log.info("Installing talus code importer")
        code = self._config["code"]
        self._code_importer = TalusCodeImporter(
            code["loc"],
            code["username"],
            code["password"],
            parent_log=self._log
        )
        sys.meta_path = [self._code_importer]


def main(dev=False):
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    bootstrap = TalusBootstrap(config_path, dev=dev)
    bootstrap.run()


if __name__ == "__main__":
    dev = False
    if len(sys.argv) > 1 and sys.argv[1] == "dev":
        dev = True

    main(dev=dev)
