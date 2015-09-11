import abc

from collections import namedtuple
from patroni.exceptions import DCSError
from patroni.utils import calculate_ttl, sleep
from six.moves.urllib_parse import urlparse, urlunparse, parse_qsl


def parse_connection_string(value):
    """Original Governor stores connection strings for each cluster members if a following format:
        postgres://{username}:{password}@{connect_address}/postgres
    Since each of our patroni instances provides own REST API endpoint it's good to store this information
    in DCS among with postgresql connection string. In order to not introduce new keys and be compatible with
    original Governor we decided to extend original connection string in a following way:
        postgres://{username}:{password}@{connect_address}/postgres?application_name={api_url}
    This way original Governor could use such connection string as it is, because of feature of `libpq` library.

    This method is able to split connection string stored in DCS into two parts, `conn_url` and `api_url`"""

    scheme, netloc, path, params, query, fragment = urlparse(value)
    conn_url = urlunparse((scheme, netloc, path, params, '', fragment))
    api_url = ([v for n, v in parse_qsl(query) if n == 'application_name'] or [None])[0]
    return conn_url, api_url


class Member(namedtuple('Member', 'index,name,conn_url,api_url,expiration,ttl')):

    """Immutable object (namedtuple) which represents single member of PostgreSQL cluster.
    Consists of the following fields:
    :param index: modification index of a given member key in a Configuration Store
    :param name: name of PostgreSQL cluster member
    :param conn_url: connection string containing host, user and password which could be used to access this member.
    :param api_url: REST API url of patroni instance
    :param expiration: expiration time of given member key
    :param ttl: ttl of given member key in seconds"""

    def real_ttl(self):
        return calculate_ttl(self.expiration) or -1


class Leader(namedtuple('Leader', 'index,expiration,ttl,member')):

    """Immutable object (namedtuple) which represents leader key.
    Consists of the following fields:
    :param index: modification index of a leader key in a Configuration Store
    :param expiration: expiration time of the leader key
    :param ttl: ttl of the leader key
    :param member: reference to a `Member` object which represents current leader (see `Cluster.members`)"""

    @property
    def name(self):
        return self.member.name

    @property
    def conn_url(self):
        return self.member.conn_url


class Cluster(namedtuple('Cluster', 'initialize,leader,last_leader_operation,members,standby')):
    """Immutable object (namedtuple) which represents PostgreSQL cluster.
    Consists of the following fields:
    :param initialize: boolean, shows whether this cluster has initialization key stored in DC or not.
    :param leader: `Leader` object which represents current leader of the cluster
    :param last_leader_operation: int or long object containing position of last known leader operation.
        This value is stored in `/optime/leader` key
    :param members: list of Member object, all PostgreSQL cluster members including leader
    :param standby: indicates whether this cluster runs a replica of an external PostgreSQL cluster"""

    def is_unlocked(self):
        return not (self.leader and self.leader.name)


class AbstractDCS:

    __metaclass__ = abc.ABCMeta

    _INITIALIZE = 'initialize'
    _LEADER = 'leader'
    _MEMBERS = 'members/'
    _OPTIME = 'optime'
    _STANDBY = 'standby'
    _LEADER_OPTIME = _OPTIME + '/' + _LEADER

    def __init__(self, name, config):
        """
        :param name: name of current instance (the same value as `~Postgresql.name`)
        :param config: dict, reference to config section of selected DCS.
            i.e.: `zookeeper` for zookeeper, `etcd` for etcd, etc...
        """
        self._name = name
        self._scope = config['scope']
        self._base_path = '/service/' + self._scope

    def client_path(self, path):
        return '/'.join([self._base_path, path.lstrip('/')])

    @property
    def initialize_path(self):
        return self.client_path(self._INITIALIZE)

    @property
    def members_path(self):
        return self.client_path(self._MEMBERS)

    @property
    def member_path(self):
        return self.client_path(self._MEMBERS + self._name)

    @property
    def leader_path(self):
        return self.client_path(self._LEADER)

    @property
    def leader_optime_path(self):
        return self.client_path(self._LEADER_OPTIME)

    @property
    def standby_path(self):
        return self.client_path(self._STANDBY)

    @abc.abstractmethod
    def get_cluster(self):
        """:returns: `Cluster` object which represent current state and topology of the cluster
        raise `~DCSError` in case of communication or other problems with DCS. If current instance was
            running as a master and exception raised instance would be demoted."""

    @abc.abstractmethod
    def update_leader(self, state_handler):
        """Update leader key (or session) ttl and `/optime/leader` key in DCS.

        :param state_handler: reference to `Postgresql` object
        :returns: `!True` if leader key (or session) has been updated successfully.
            If not, `!False` must be returned and current instance would be demoted.

        If you failed to update `/optime/leader` this error is not critical and you can return `!True`
        You have to use CAS (Compare And Swap) operation in order to update leader key,
        for example for etcd `prevValue` parameter must be used."""

    @abc.abstractmethod
    def attempt_to_acquire_leader(self):
        """Attempt to acquire leader lock
        This method should create `/leader` key with value=`~self._name`
        :returns: `!True` if key has been created successfully.

        Key must be created atomically. In case if key already exists it should not be
        overwritten and `!False` must be returned"""

    def current_leader(self):
        try:
            cluster = self.get_cluster()
            return None if cluster.is_unlocked() else cluster.leader
        except DCSError:
            return None

    @abc.abstractmethod
    def touch_member(self, connection_string, ttl=None, permanent=False, name=None):
        """Update member key in DCS.
        This method should create or update key with the name = '/members/' + `~self._name`
        and value = connection_string in a given DCS. Both 'permanent' and 'name' parameters
        are using in the cluster standby mode for creating a record corresponding to the
        external master.

        :param connection_string: how this instance can be accessed by other instances
        :param ttl: ttl for member key, optional parameter. If it is None `~self.member_ttl will be used`
        :param permanent: do not use TTL for a member key
        :param name: supply a different name as contents of a member key
        :returns: `!True` on success otherwise `!False`
        """

    @abc.abstractmethod
    def take_leader(self, permanent=False, name=None):
        """This method should create leader key with value = `~self._name` and ttl=`~self.ttl`
        Since it could be called only on initial cluster bootstrap it could create this key regardless,
        overwriting the key if necessary. Both 'permanent' and 'name' parameters
        are using in the cluster standby mode for creating a record corresponding to the
        external master.

        :param permanent: do not use TTL for a leader key.
        :param name: supply a different node name as a contents of the leader key

        """

    @abc.abstractmethod
    def initialize(self):
        """Race for cluster initialization.
        :param path: usually this is just '/initialize'
        :returns: `!True` if key has been created successfully.

        this method should create atomically `path` key and return `!True`
        otherwise it should return `!False`"""

    @abc.abstractmethod
    def delete_leader(self):
        """Voluntarily remove leader key from DCS
        This method should remove leader key if current instance is the leader"""

    @abc.abstractmethod
    def cancel_initialization(self):
        """ Removes the initialize key for a cluster """

    @abc.abstractmethod
    def set_standby(self):
        """ Sets the standby mode flag for a cluster """

    @abc.abstractmethod
    def clear_standby(self):
        """ Clears the standby mode for a cluster """

    @abc.abstractmethod
    def get_standby(self):
        """ Returns whether the standby mode is turned on """

    @property
    def standby(self):
        return self.get_standby()

    def watch(self, timeout):
        sleep(timeout)
