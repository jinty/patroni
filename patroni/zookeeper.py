import logging
import random
import requests
import time

from kazoo.client import KazooClient, KazooState
from kazoo.exceptions import NoNodeError, NodeExistsError, KazooException
from patroni.dcs import AbstractDCS, Cluster, DCSError, Leader, Member, parse_connection_string
from patroni.utils import sleep
from requests.exceptions import RequestException

logger = logging.getLogger(__name__)


class ZooKeeperError(DCSError):
    pass


class ExhibitorEnsembleProvider:

    TIMEOUT = 3.1

    def __init__(self, hosts, port, uri_path='/exhibitor/v1/cluster/list', poll_interval=300):
        self._exhibitor_port = port
        self._uri_path = uri_path
        self._poll_interval = poll_interval
        self._exhibitors = hosts
        self._master_exhibitors = hosts
        self._zookeeper_hosts = ''
        self._next_poll = None
        while not self.poll():
            logger.info('waiting on exhibitor')
            sleep(5)

    def poll(self):
        if self._next_poll and self._next_poll > time.time():
            return False

        json = self._query_exhibitors(self._exhibitors)
        if not json:
            json = self._query_exhibitors(self._master_exhibitors)

        if isinstance(json, dict) and 'servers' in json and 'port' in json:
            self._next_poll = time.time() + self._poll_interval
            zookeeper_hosts = ','.join([h + ':' + str(json['port']) for h in sorted(json['servers'])])
            if self._zookeeper_hosts != zookeeper_hosts:
                logger.info('ZooKeeper connection string has changed: %s => %s', self._zookeeper_hosts, zookeeper_hosts)
                self._zookeeper_hosts = zookeeper_hosts
                self._exhibitors = json['servers']
                return True
        return False

    def _query_exhibitors(self, exhibitors):
        random.shuffle(exhibitors)
        for host in exhibitors:
            uri = 'http://{}:{}{}'.format(host, self._exhibitor_port, self._uri_path)
            try:
                response = requests.get(uri, timeout=self.TIMEOUT)
                return response.json()
            except RequestException:
                pass
        return None

    @property
    def zookeeper_hosts(self):
        return self._zookeeper_hosts


class ZooKeeper(AbstractDCS):

    def __init__(self, name, config):
        super(ZooKeeper, self).__init__(name, config)

        hosts = config.get('hosts', [])
        if isinstance(hosts, list):
            hosts = ','.join(hosts)

        self.exhibitor = None
        if 'exhibitor' in config:
            exhibitor = config['exhibitor']
            interval = exhibitor.get('poll_interval', 300)
            self.exhibitor = ExhibitorEnsembleProvider(exhibitor['hosts'], exhibitor['port'], poll_interval=interval)
            hosts = self.exhibitor.zookeeper_hosts

        self.client = KazooClient(hosts=hosts,
                                  timeout=(config.get('session_timeout', None) or 30),
                                  command_retry={
                                      'deadline': (config.get('reconnect_timeout', None) or 10),
                                      'max_delay': 1,
                                      'max_tries': -1},
                                  connection_retry={'max_delay': 1, 'max_tries': -1})
        self.client.add_listener(self.session_listener)
        self.cluster_event = self.client.handler.event_object()

        self.fetch_cluster = True
        self.members = []
        self.leader = None
        self.last_leader_operation = 0
        self._standby = False

        self.client.start(None)

    def session_listener(self, state):
        if state in [KazooState.SUSPENDED, KazooState.LOST]:
            self.cluster_watcher(None)

    def cluster_watcher(self, event):
        self.fetch_cluster = True
        self.cluster_event.set()

    def get_node(self, key, watch=None):
        try:
            return self.client.get(key, watch)
        except NoNodeError:
            pass
        except:
            logger.exception('get_node')
        return None

    @staticmethod
    def member(name, value, znode):
        conn_url, api_url = parse_connection_string(value)
        return Member(znode.mzxid, name, conn_url, api_url, None, None)

    def load_members(self):
        members = []
        for member in self.client.get_children(self.members_path, self.cluster_watcher):
            data = self.get_node(self.member_path)
            if data is not None:
                members.append(self.member(member, *data))
        return members

    def _inner_load_cluster(self):
        self.cluster_event.clear()
        leader = self.get_node(self.leader_path, self.cluster_watcher)
        self.members = self.load_members()
        if leader:
            client_id = self.client.client_id
            if leader[0] == self._name and client_id is not None and client_id[0] != leader[1].ephemeralOwner:
                logger.info('I am leader but not owner of the session. Removing leader node')
                self.client.delete(self.leader_path)
                leader = None

            if leader:
                member = Member(-1, leader[0], None, None, None, None)
                member = ([m for m in self.members if m.name == leader[0]] or [member])[0]
                leader = Leader(leader[1].mzxid, None, None, member)
                self.fetch_cluster = member.index == -1

        self.leader = leader
        if self.fetch_cluster:
            last_leader_operation = self.get_node(self.leader_optime_path)
            if last_leader_operation:
                self.last_leader_operation = int(last_leader_operation[0])

        self._standby = self.standby

    def get_cluster(self):
        if self.exhibitor and self.exhibitor.poll():
            self.client.set_hosts(self.exhibitor.zookeeper_hosts)

        if self.fetch_cluster:
            try:
                self.client.retry(self._inner_load_cluster)
            except:
                logger.exception('get_cluster')
                self.session_listener(KazooState.LOST)
                raise ZooKeeperError('ZooKeeper in not responding properly')
        return Cluster(True, self.leader, self.last_leader_operation, self.members, self._standby)

    def _create(self, path, value, **kwargs):
        try:
            self.client.retry(self.client.create, path, value, **kwargs)
            return True
        except:
            return False

    def attempt_to_acquire_leader(self, permanent=False, name=None):
        name = name or self._name
        ret = self._create(self.leader_path, name, makepath=True, ephemeral=(not permanent))
        ret or logger.info('Could not take out TTL lock')
        return ret

    def initialize(self):
        return self._create(self.initialize_path, self._name, makepath=True)

    def touch_member(self, connection_string, ttl=None, permanent=False, name=None):
        for m in self.members:
            if m.name == (self._name if not name else name):
                return True
        member_path = self.client_path(self._MEMBERS + name) if name else self.member_path
        try:
            self.client.retry(self.client.create, member_path, connection_string, makepath=True,
                              ephemeral=(not permanent))
            return True
        except NodeExistsError:
            try:
                self.client.retry(self.client.delete, member_path)
                self.client.retry(self.client.create, member_path, connection_string, makepath=True,
                                  ephemeral=(not permanent))
                return True
            except:
                logger.exception('touch_member')
        return False

    def take_leader(self, permanent=False, name=None):
        return self.attempt_to_acquire_leader(permanent, name)

    def update_leader(self, state_handler):
        last_operation = state_handler.last_operation()
        if last_operation != self.last_leader_operation:
            self.last_leader_operation = last_operation
            path = self.leader_optime_path
            try:
                self.client.retry(self.client.set, path, last_operation)
            except NoNodeError:
                try:
                    self.client.retry(self.client.create, path, last_operation, makepath=True)
                except:
                    logger.exception('Failed to create %s', path)
            except:
                logger.exception('Failed to update %s', path)
        return True

    def delete_leader(self):
        if isinstance(self.leader, Leader) and self.leader.name == self._name:
            self.client.delete(self.leader_path)

    def cancel_initialization(self):
        node = self.get_node(self.initialize_path)
        if node and node[0] == self._name:
            try:
                self.client.retry(self.client.delete, self.initialize_path, version=node[1].mzxid)
            except KazooException:
                logger.exception("Unable to delete initialize key")

    def watch(self, timeout):
        self.cluster_event.wait(timeout)
        if self.cluster_event.isSet():
            self.fetch_cluster = True

    def set_standby(self):
        return self._create(self.standby_path, self._name, makepath=True, ephemeral=True)

    def clear_standby(self):
        node = self.get_node(self.standby_path)
        if node and node[0] == self._name:
            try:
                self.client.retry(self.client.delete, self.standby_path, version=node[1].mzxid)
            except KazooException:
                logger.exception("Unable to delete standby key")

    def get_standby(self):
        return self.get_node(self.standby_path) is not None

    @property
    def standby(self):
        return self.get_standby()
