# Copyright ClusterHQ Inc.  See LICENSE file for details.

"""
Tests for :module:`flocker.node.script`.
"""

from functools import wraps
import logging
import socket
from unittest import skipUnless
from uuid import uuid4

import yaml
from ipaddr import IPAddress

from pyrsistent import PClass, field

from jsonschema.exceptions import ValidationError

from eliot.testing import assertHasAction, capture_logging

from zope.interface.verify import verifyObject

from twisted.internet.defer import Deferred
from twisted.python.filepath import FilePath
from twisted.application.service import Service
from twisted.python.runtime import platform
from twisted.python.usage import UsageError

from ...common.script import ICommandLineScript
from ...common.plugin import PluginNotFound
from ...common import get_all_ips
from ...common._era import get_era

from ..script import (
    AgentScript, ContainerAgentOptions,
    AgentServiceFactory, DatasetAgentOptions, validate_configuration,
    _context_factory_and_credential, DatasetServiceFactory,
    AgentService, get_configuration,
    DeployerType, _get_external_ip, LOG_GET_EXTERNAL_IP
)
from ..backends import BackendDescription, LOOPBACK, ZFS

from .._loop import AgentLoopService
from ...testtools import MemoryCoreReactor, TestCase, random_name
from ...ca.testtools import get_credential_sets

from .dummybackend import DUMMY_API


def setup_config(test, control_address=u"10.0.0.1", control_port=1234,
                 name=None, log_config=None):
    """
    Create a configuration file and certificates for a dataset agent in a
    temporary directory.

    Sets ``config`` attribute on the test instance with the path to the config
    file.

    :param test: A ``TestCase`` instance.
    :param unicode control_address: The address of the control service.
    :param int control_port: The port number of the control service.
    :param unicode name: The ZFS pool name.  If ``None``, a random one will be
        generated that identifies the given test.
    :param dict log_config: A logging configuration dictionary. If ``None``,
        no logging stanza will be added to the configuration file.
    """
    if name is None:
        name = random_name(test)
    ca_set = get_credential_sets()[0]
    scratch_directory = test.make_temporary_directory()
    contents = {
        u"control-service": {
            u"hostname": control_address,
            u"port": control_port,
        },
        u"dataset": {
            u"backend": u"zfs",
            u"name": name,
            u"mount_root": scratch_directory.child(b"mount_root").path,
            u"volume_config_path": scratch_directory.child(
                b"volume_config.json"
            ).path,
        },
        u"version": 1,
    }
    if log_config is not None:
        contents[u'logging'] = log_config
    test.config = scratch_directory.child('dataset-config.yml')
    test.config.setContent(yaml.safe_dump(contents))
    ca_set.copy_to(scratch_directory, node=True)
    test.ca_set = ca_set

deployer = object()


# This should have an explicit interface:
# https://clusterhq.atlassian.net/browse/FLOC-1929
def deployer_factory_stub(**kw):
    if set(kw.keys()) != {"node_uuid", "cluster_uuid", "hostname"}:
        raise TypeError("wrong arguments")
    return deployer


class DummyAgentService(PClass):
    reactor = field()
    loop_service = field()
    configuration = field()

    @classmethod
    def for_loop_service(cls, loop_service):
        return lambda configuration: cls(
            loop_service=loop_service,
            configuration=configuration,
        )

    def get_api(self):
        return None

    def get_deployer(self, api):
        return None

    def get_loop_service(self, deployer):
        return self.loop_service


class DatasetServiceFactoryTests(TestCase):
    """
    Generic tests for ``DatasetServiceFactory``, independent of the storage
    driver being used.
    """

    def test_config_validated(self):
        """
        ``DatasetServiceFactory.get_service`` validates the configuration file.
        """
        config = self.make_temporary_file(content=b"INVALID")

        options = DatasetAgentOptions()
        options.parseOptions([b"--agent-config", config.path])

        self.assertRaises(
            ValidationError,
            DatasetServiceFactory().get_service, MemoryCoreReactor(), options,
        )

    def test_missing_configuration_file(self):
        """
        ``DatasetServiceFactory.get_service`` raises an ``IOError`` if the
        given configuration file does not exist.
        """
        options = DatasetAgentOptions()
        options.parseOptions(
            [b"--agent-config", self.make_temporary_path().path]
        )

        self.assertRaises(
            IOError,
            DatasetServiceFactory().get_service, MemoryCoreReactor(), options,
        )


def agent_service_setup(test):
    """
    Do some setup common to all of the ``AgentService`` test cases.

    Various attributes will be set on ``test`` for use by the test method.

    :param test: A ``TestCase`` instance.
    """
    test.ca_set = get_credential_sets()[0]

    test.host = b"192.0.2.5"
    test.port = 54123
    test.reactor = MemoryCoreReactor()

    test.agent_service = AgentService(
        reactor=test.reactor,

        control_service_host=test.host,
        control_service_port=test.port,
        node_credential=test.ca_set.node,
        ca_certificate=test.ca_set.root.credential.certificate,

        backend_description=LOOPBACK,
        api_args={},
    )


def _restore_logging(log_name='flocker.test'):
    """
    Save and restore a logging configuration.
    """
    def decorator(function):
        @wraps(function)
        def wrapper(self):
            def restore_logger(logger, level, handlers):
                logger.setLevel(level)
                for handler in logger.handlers[:]:
                    logger.removeHandler(handler)
                for handler in handlers:
                    logger.addHandler(handler)

            logger = logging.getLogger(log_name)
            self.addCleanup(
                restore_logger, logger, logger.getEffectiveLevel(),
                logger.handlers[:]
            )

            return function(self, log_name)
        return wrapper
    return decorator


class AgentServiceFromConfigurationTests(TestCase):
    """
    Tests for ``AgentService.from_configuration``
    """
    def test_initialized(self):
        """
        ``AgentService.from_configuration`` returns an ``AgentService``
        instance with all of its fields initialized from parts of the
        configuration passed in.
        """
        host = b"192.0.2.13"
        port = 2314
        name = u"from_config-test"

        setup_config(self, control_address=host, control_port=port, name=name)
        options = DatasetAgentOptions()
        options.parseOptions([b"--agent-config", self.config.path])
        config = get_configuration(options)
        agent_service = AgentService.from_configuration(config)
        self.assertEqual(
            AgentService(
                control_service_host=host,
                control_service_port=port,

                # Compare this separately :/
                node_credential=None,
                ca_certificate=self.ca_set.root.credential.certificate,
                backend_description=ZFS,
                api_args={
                    "name": name,
                    "mount_root": self.config.sibling(b"mount_root").path,
                    "volume_config_path": self.config.sibling(
                        b"volume_config.json"
                    ).path,
                },
            ),
            agent_service.set(node_credential=None),
        )
        # The credentials differ only by the path they were loaded from.
        self.assertEqual(
            self.ca_set.node.transform(["credential", "path"], None),
            agent_service.node_credential.transform(
                ["credential", "path"], None
            ),
        )

    @_restore_logging(log_name='flocker.test')
    def test_logging(self, log_name):
        """
        Logging is configured by a logging stanza.
        """
        # Setup an AgentService with a logging stanza
        host = b"192.0.2.13"
        port = 2314
        name = u"from_config-test"
        logfile = self.make_temporary_path()
        log_config = {
            'version': 1,
            'handlers': {
                'logfile': {
                    'class': 'logging.FileHandler',
                    'level': 'DEBUG',
                    'filename': logfile.path,
                    'encoding': 'utf-8',
                }
            },
            'loggers': {
                log_name: {
                    'handlers': ['logfile'],
                    'level': 'DEBUG',
                },
            },
        }
        setup_config(
            self, control_address=host, control_port=port, name=name,
            log_config=log_config
        )
        options = DatasetAgentOptions()
        options.parseOptions([b"--agent-config", self.config.path])
        config = get_configuration(options)
        AgentService.from_configuration(config)

        # Root logger now logs to file
        log_message = 'My LoG tEsT.'
        logger = logging.getLogger(log_name)
        logger.info(log_message)
        self.assertIn(log_message, logfile.getContent())


class AgentServiceGetAPITests(TestCase):
    """
    Tests for ``AgentService.get_api``.
    """
    def setUp(self):
        super(AgentServiceGetAPITests, self).setUp()
        agent_service_setup(self)

    def test_backend_selection(self):
        """
        ``AgentService.get_api`` returns an object constructed by the
        factory corresponding to the agent's ``backend_description``,
        supplying ``api_args``.
        """
        class API(PClass):
            a = field()
            b = field()

        agent_service = self.agent_service.set(
            "backend_description",
            BackendDescription(
                name=u"foo", needs_reactor=False, needs_cluster_id=False,
                api_factory=API, deployer_type=DeployerType.p2p,
            ),
        ).set(
            "api_args", {"a": "x", "b": "y"},
        )

        api = agent_service.get_api()
        self.assertEqual(
            API(a="x", b="y"),
            api,
        )

    def test_needs_reactor(self):
        """
        If the flag for needing a reactor as an extra argument is set in the
        backend_description, the ``AgentService`` passes its own reactor when
        ``AgentService.get_api`` calls the backend factory.
        """
        reactor = MemoryCoreReactor()

        class API(PClass):
            reactor = field()

        agent_service = self.agent_service.set(
            "backend_description",
            BackendDescription(
                name=u"foo",
                needs_reactor=True, needs_cluster_id=False,
                api_factory=API, deployer_type=DeployerType.p2p,
            ),
        ).set(
            "reactor", reactor,
        )
        api = agent_service.get_api()
        self.assertEqual(
            API(reactor=reactor),
            api,
        )

    def test_needs_cluster_id(self):
        """
        If the flag for needing a cluster id as an extra argument is set in the
        backend_description, the ``AgentService`` passes the cluster id
        extracted from the node certificate when ``AgentService.get_api`` calls
        the backend factory.
        """
        class API(PClass):
            cluster_id = field()

        agent_service = self.agent_service.set(
            "backend_description",
            BackendDescription(
                name=u"foo",
                needs_reactor=False, needs_cluster_id=True,
                api_factory=API, deployer_type=DeployerType.p2p,
            ),
        )
        api = agent_service.get_api()
        self.assertEqual(
            API(cluster_id=self.ca_set.node.cluster_uuid),
            api,
        )

    def test_required_config(self):
        """
        A ``UsageError`` is raised if the loaded configuration for the API
        factory does not contain a key in the corresponding backend's required
        configuration keys.
        """
        class API(PClass):
            region = field()
            api_key = field()

        agent_service = self.agent_service.set(
            "backend_description",
            BackendDescription(
                name=u"foo",
                needs_reactor=False, needs_cluster_id=False,
                api_factory=API, deployer_type=DeployerType.p2p,
                required_config={u"region", u"api_key"},
            ),
        )
        agent_service = agent_service.set("api_args", {
            "region": "abc",
        })
        error = self.assertRaises(UsageError, agent_service.get_api)
        self.assertEqual(
            error.message,
            u'Configuration error: Required key api_key is missing.'
        )

    def test_3rd_party_backend(self):
        """
        If the backend name is not that of a pre-configured backend, the
        backend name is treated as a Python import path, and the
        ``FLOCKER_BACKEND`` attribute of that is used as the backend.
        """
        agent_service = AgentService.from_configuration(
            configuration={
                u"control-service": {
                    u"hostname": b"192.0.2.1",
                    u"port": 1234,
                },
                u"node-credential": type(
                    "node-credential", (object,), {"cluster_uuid": uuid4()}
                )(),
                u"ca-certificate": None,
                u"dataset": {
                    u"backend": u"flocker.node.test.dummybackend",
                    u"custom": u"arguments!"
                },
            },
            reactor=MemoryCoreReactor(),
        )
        api = agent_service.get_api()
        # This backend is hardcoded to always return the same object:
        self.assertIs(api, DUMMY_API)

    def test_wrong_attribute_3rd_party_backend(self):
        """
        If the backend name refers to a bad attribute lookup path in an
        importable package, an appropriate ``ValueError`` is raised.
        """
        self.assertRaises(
            PluginNotFound,
            AgentService.from_configuration,
            configuration={
                u"control-service": {
                    u"hostname": b"192.0.2.1",
                    u"port": 1234,
                },
                u"node-credential": type(
                    "node-credential", (object,), {"cluster_uuid": uuid4()}
                )(),
                u"ca-certificate": None,
                u"dataset": {
                    u"backend": u"flocker.not.a.real.module",
                    u"custom": u"arguments!"
                },
            },
            reactor=MemoryCoreReactor(),
        )

    def test_wrong_package_3rd_party_backend(self):
        """
        If the backend name refers to an unimportable package, an appropriate
        ``ValueError`` is raised.
        """
        self.assertRaises(
            PluginNotFound,
            AgentService.from_configuration,
            configuration={
                u"control-service": {
                    u"hostname": b"192.0.2.1",
                    u"port": 1234,
                },
                u"node-credential": type(
                    "node-credential", (object,), {"cluster_uuid": uuid4()}
                )(),
                u"ca-certificate": None,
                u"dataset": {
                    u"backend": u"notarealmoduleireallyhope",
                    u"custom": u"arguments!"
                },
            },
            reactor=MemoryCoreReactor(),
        )


class AgentServiceDeployerTests(TestCase):
    """
    Tests for ``AgentService.get_deployer``.
    """
    def setUp(self):
        super(AgentServiceDeployerTests, self).setUp()
        agent_service_setup(self)

    def test_backend_selection(self):
        """
        ``AgentService.get_deployer`` finds a factory the configured backend in
        its ``deployers`` dictionary and uses it to create the new
        ``IDeployer`` provider with the supplied API object and the local
        host's external IP address and unique node identifier.
        """
        ip = b"192.0.2.7"
        ips = {
            (self.agent_service.control_service_host,
             self.agent_service.control_service_port): ip,
        }

        class Deployer(PClass):
            api = field(mandatory=True)
            hostname = field(mandatory=True)
            node_uuid = field(mandatory=True)

        class WrongDeployer(PClass):
            pass

        def get_external_ip(host, port):
            return ips[host, port]

        agent_service = self.agent_service.set(
            "get_external_ip", get_external_ip,
        ).set(
            "backend_description",
            BackendDescription(
                name=u"foo",
                needs_reactor=False, needs_cluster_id=False,
                api_factory=None, deployer_type=DeployerType.p2p,
            ),
        ).set(
            "deployers", {
                DeployerType.p2p: Deployer,
                DeployerType.block: WrongDeployer,
            },
        )

        api = object()
        deployer = agent_service.get_deployer(api)
        self.assertEqual(
            Deployer(
                api=api,
                hostname=ip,
                node_uuid=self.ca_set.node.uuid,
            ),
            deployer,
        )


class AgentServiceLoopTests(TestCase):
    """
    Tests for ``AgentService.get_loop_service``.
    """
    def setUp(self):
        super(AgentServiceLoopTests, self).setUp()
        agent_service_setup(self)

    @skipUnless(platform.isLinux(), "get_era() only supports Linux.")
    def test_agentloopservice(self):
        """
        ```AgentService.get_loop_service`` returns an ``AgentLoopService``
        using the given deployer and all of the configuration supplied to
        the``AgentService``.
        """
        deployer = object()
        loop_service = self.agent_service.get_loop_service(deployer)
        context_factory = self.agent_service.get_tls_context().context_factory
        self.assertEqual(
            AgentLoopService(
                reactor=self.reactor,
                deployer=deployer,
                host=self.host,
                port=self.port,
                context_factory=context_factory,
                era=get_era()
            ),
            loop_service,
        )


class AgentServiceFactoryTests(TestCase):
    """
    Tests for ``AgentServiceFactory``.
    """
    def setUp(self):
        super(AgentServiceFactoryTests, self).setUp()
        setup_config(self)

    def service_factory(self, deployer_factory):
        """
        Create a new ``AgentServiceFactory`` suitable for unit-testing.

        :param deployer_factory: ``deployer_factory`` to use.

        :return: ``AgentServiceFactory`` instance.
        """
        return AgentServiceFactory(
            deployer_factory=deployer_factory,
            get_external_ip=lambda host, port: u"127.0.0.1")

    def test_uuids_from_certificate(self):
        """
        The created deployer got its node UUID and cluster UUID from the given
        node certificate.
        """
        result = []

        def factory(hostname, node_uuid, cluster_uuid):
            result.append((node_uuid, cluster_uuid))
            return object()

        options = DatasetAgentOptions()
        options.parseOptions([b"--agent-config", self.config.path])
        service_factory = self.service_factory(deployer_factory=factory)
        service_factory.get_service(MemoryCoreReactor(), options)
        self.assertEqual(
            (self.ca_set.node.uuid,
             self.ca_set.node.cluster_uuid),
            result[0])

    @skipUnless(platform.isLinux(), "get_era() only supports Linux.")
    def test_get_service(self):
        """
        ``AgentServiceFactory.get_service`` creates an ``AgentLoopService``
        configured with the destination given in the config file given by the
        options.
        """
        reactor = MemoryCoreReactor()
        options = DatasetAgentOptions()
        options.parseOptions([b"--agent-config", self.config.path])
        service_factory = self.service_factory(
            deployer_factory=deployer_factory_stub,
        )
        self.assertEqual(
            AgentLoopService(
                reactor=reactor,
                deployer=deployer,
                host=b"10.0.0.1",
                port=1234,
                context_factory=_context_factory_and_credential(
                    self.config.parent(), b"10.0.0.1", 1234).context_factory,
                era=get_era(),
            ),
            service_factory.get_service(reactor, options)
        )

    @skipUnless(platform.isLinux(), "get_era() only supports Linux.")
    def test_default_port(self):
        """
        ``AgentServiceFactory.get_service`` creates an ``AgentLoopService``
        configured with port 4524 if no port is specified.
        """
        self.config.setContent(
            yaml.safe_dump({
                u"control-service": {
                    u"hostname": u"10.0.0.2",
                },
                u"dataset": {
                    u"backend": u"zfs",
                },
                u"version": 1,
            }))

        reactor = MemoryCoreReactor()
        options = DatasetAgentOptions()
        options.parseOptions([b"--agent-config", self.config.path])
        service_factory = self.service_factory(
            deployer_factory=deployer_factory_stub,
        )
        self.assertEqual(
            AgentLoopService(
                reactor=reactor,
                deployer=deployer,
                host=b"10.0.0.2",
                port=4524,
                context_factory=_context_factory_and_credential(
                    self.config.parent(), b"10.0.0.2", 4524).context_factory,
                era=get_era(),
            ),
            service_factory.get_service(reactor, options)
        )

    def test_config_validated(self):
        """
        ``AgentServiceFactory.get_service`` validates the configuration file.
        """
        self.config.setContent("INVALID")
        reactor = MemoryCoreReactor()
        options = DatasetAgentOptions()
        options.parseOptions([b"--agent-config", self.config.path])
        service_factory = self.service_factory(
            deployer_factory=deployer_factory_stub,
        )

        self.assertRaises(
            ValidationError,
            service_factory.get_service, reactor, options,
        )

    def test_deployer_factory_called_with_ip(self):
        """
        ``AgentServiceFactory.main`` calls its ``deployer_factory`` with one
        of the node's IPs.
        """
        spied = []

        def deployer_factory(node_uuid, hostname, cluster_uuid):
            spied.append(IPAddress(hostname))
            return object()

        reactor = MemoryCoreReactor()
        options = DatasetAgentOptions()
        options.parseOptions([b"--agent-config", self.config.path])
        agent = self.service_factory(deployer_factory=deployer_factory)
        agent.get_service(reactor, options)
        self.assertIn(spied[0], get_all_ips())

    def test_missing_configuration_file(self):
        """
        ``AgentServiceFactory.get_service`` raises an ``IOError`` if the given
        configuration file does not exist.
        """
        reactor = MemoryCoreReactor()
        options = DatasetAgentOptions()
        options.parseOptions(
            [b"--agent-config", self.make_temporary_path().path]
        )
        service_factory = self.service_factory(
            deployer_factory=deployer_factory_stub,
        )

        self.assertRaises(
            IOError,
            service_factory.get_service, reactor, options,
        )


class AgentScriptTests(TestCase):
    """
    Tests for ``AgentScript``.
    """
    def setUp(self):
        super(AgentScriptTests, self).setUp()
        self.reactor = MemoryCoreReactor()
        self.options = DatasetAgentOptions()

    def test_interface(self):
        """
        ``AgentScript`` instances provide ``ICommandLineScript``.
        """
        self.assertTrue(
            verifyObject(
                ICommandLineScript,
                AgentScript(
                    service_factory=lambda reactor, options: Service()
                )
            )
        )

    def test_service_factory_called_with_main_arguments(self):
        """
        ``AgentScript`` calls the ``service_factory`` with the reactor
        and options passed to ``AgentScript.main``.
        """
        args = []
        service = Service()

        def service_factory(reactor, options):
            args.append((reactor, options))
            return service

        agent = AgentScript(service_factory=service_factory)
        agent.main(self.reactor, self.options)
        self.assertEqual([(self.reactor, self.options)], args)

    def test_main_starts_service(self):
        """
        ```AgentScript.main`` starts the service created by its
        ``service_factory`` .
        """
        service = Service()
        agent = AgentScript(
            service_factory=lambda reactor, options: service
        )
        agent.main(self.reactor, self.options)
        self.assertTrue(service.running)

    def test_main_stops_service(self):
        """
        When the reactor passed to ``AgentScript.main`` shuts down, the
        service created by the ``service_factory`` is stopped.
        """
        service = Service()
        agent = AgentScript(
            service_factory=lambda reactor, options: service
        )
        agent.main(self.reactor, self.options)
        self.reactor.fireSystemEvent("shutdown")
        self.assertFalse(service.running)

    def test_main_deferred_fires_after_service_stop(self):
        """
        The ``Deferred`` returned by ``AgentScript.main`` doesn't fire
        until after the ``Deferred`` returned by the ``stopService`` method of
        the service created by ``service_factory``.
        """
        shutdown_deferred = Deferred()

        class SlowShutdown(Service):
            def stopService(self):
                return shutdown_deferred

        service = SlowShutdown()
        agent = AgentScript(
            service_factory=lambda reactor, options: service
        )
        stop_deferred = agent.main(self.reactor, self.options)
        self.reactor.fireSystemEvent("shutdown")
        self.assertNoResult(stop_deferred)
        shutdown_deferred.callback(None)
        self.assertIs(None, self.successResultOf(stop_deferred))


def make_amp_agent_options_tests(options_type):
    """
    Create a test case which contains the tests that should apply to any and
    all convergence agents (dataset or container).

    :param options_type: An ``Options`` subclass  to be tested.

    :return: A ``TestCase`` subclass defining tests for that options
        type.
    """

    class Tests(TestCase):
        def setUp(self):
            super(Tests, self).setUp()
            self.options = options_type()
            self.scratch_directory = FilePath(self.mktemp())
            self.scratch_directory.makedirs()
            self.sample_content = yaml.safe_dump({
                u"control-service": {
                    u"hostname": u"10.0.0.1",
                    u"port": 4524,
                },
                u"version": 1,
            })
            self.config = self.scratch_directory.child('dataset-config.yml')
            self.config.setContent(self.sample_content)

        def test_default_config_file(self):
            """
            The default config file is a FilePath with path
            ``/etc/flocker/agent.yml``.
            """
            self.options.parseOptions([])
            self.assertEqual(
                self.options["agent-config"],
                FilePath("/etc/flocker/agent.yml"))

        def test_custom_config_file(self):
            """
            The ``--config-file`` command-line option allows configuring
            the config file.
            """
            self.options.parseOptions(
                [b"--agent-config", b"/etc/foo.yml"])
            self.assertEqual(
                self.options["agent-config"],
                FilePath("/etc/foo.yml"))

    return Tests


class ValidateConfigurationTests(TestCase):
    """
    Tests for :func:`validate_configuration`.
    """

    def setUp(self):
        super(ValidateConfigurationTests, self).setUp()
        # This is a sample working configuration which tests can modify.
        self.configuration = {
            u"control-service": {
                u"hostname": u"10.0.0.1",
                u"port": 1234,
            },
            u"dataset": {
                u"backend": u"zfs",
                u"pool": u"custom-pool",
            },
            "version": 1,
        }

    def test_valid_zfs_configuration(self):
        """
        No exception is raised when validating a valid configuration with a
        ZFS backend.
        """
        # Nothing is raised
        validate_configuration(self.configuration)

    def test_valid_loopback_configuration(self):
        """
        No exception is raised when validating a valid configuration with a
        loopback backend.
        """
        self.configuration['dataset'] = {
            u"backend": u"loopback",
            u"root_path": u"/tmp",
            u"compute_instance_id": u"42",
        }
        # Nothing is raised
        validate_configuration(self.configuration)

    def test_port_optional(self):
        """
        The control service agent's port is optional.
        """
        self.configuration['control-service'].pop('port')
        # Nothing is raised
        validate_configuration(self.configuration)

    def test_error_on_invalid_configuration_type(self):
        """
        A ``ValidationError`` is raised if the config file is not formatted
        as a dictionary.
        """
        self.configuration = "INVALID"
        self.assertRaises(
            ValidationError, validate_configuration, self.configuration)

    def test_error_on_invalid_hostname(self):
        """
        A ``ValidationError`` is raised if the given control service
        hostname is not a valid hostname.
        """
        self.configuration['control-service']['hostname'] = u"-1"
        self.assertRaises(
            ValidationError, validate_configuration, self.configuration)

    def test_error_on_missing_control_service(self):
        """
        A ``ValidationError`` is raised if the config file does not
        contain a ``u"control-service"`` key.
        """
        self.configuration.pop('control-service')
        self.assertRaises(
            ValidationError, validate_configuration, self.configuration)

    def test_error_on_missing_hostname(self):
        """
        A ``ValidationError`` is raised if the config file does not
        contain a hostname in the ``u"control-service"`` key.
        """
        self.configuration['control-service'].pop('hostname')
        self.assertRaises(
            ValidationError, validate_configuration, self.configuration)

    def test_error_on_missing_version(self):
        """
        A ``ValidationError`` is raised if the config file does not contain
        a ``u"version"`` key.
        """
        self.configuration.pop('version')
        self.assertRaises(
            ValidationError, validate_configuration, self.configuration)

    def test_error_on_high_version(self):
        """
        A ``ValidationError`` is raised if the version specified is greater
        than 1.
        """
        self.configuration['version'] = 2
        self.assertRaises(
            ValidationError, validate_configuration, self.configuration)

    def test_error_on_low_version(self):
        """
        A ``ValidationError`` is raised if the version specified is lower
        than 1.
        """
        self.configuration['version'] = 0
        self.assertRaises(
            ValidationError, validate_configuration, self.configuration)

    def test_error_on_invalid_port(self):
        """
        The control service agent's port must be an integer.
        """
        self.configuration['control-service']['port'] = 1.1
        self.assertRaises(
            ValidationError, validate_configuration, self.configuration)

    def test_error_on_missing_dataset(self):
        """
        A ``ValidationError`` is raised if the config file does not contain
        a ``u"dataset"`` key.
        """
        self.configuration.pop('dataset')
        self.assertRaises(
            ValidationError, validate_configuration, self.configuration)

    def test_error_on_missing_dataset_backend(self):
        """
        The dataset key must contain a backend type.
        """
        self.configuration['dataset'] = {}
        self.assertRaises(
            ValidationError, validate_configuration, self.configuration)

    def test_error_on_invalid_dataset_type(self):
        """
        The dataset key must contain a valid dataset type.
        """
        self.configuration['dataset'] = "invalid"
        self.assertRaises(
            ValidationError, validate_configuration, self.configuration)


class DatasetAgentOptionsTests(
        make_amp_agent_options_tests(DatasetAgentOptions)
):
    """
    Tests for ``DatasetAgentOptions``.
    """


class ContainerAgentOptionsTests(
        make_amp_agent_options_tests(ContainerAgentOptions)
):
    """
    Tests for ``ContainerAgentOptions``.
    """


class GetExternalIPTests(TestCase):
    """
    Tests for ``_get_external_ip``.
    """
    def setUp(self):
        super(GetExternalIPTests, self).setUp()
        server = socket.socket()
        server.bind(('127.0.0.1', 0))
        server.listen(5)
        self.destination_port = server.getsockname()[1]
        self.addCleanup(server.close)

    @capture_logging(lambda test, logger:
                     assertHasAction(test, logger, LOG_GET_EXTERNAL_IP, True,
                                     {u"host": u"localhost",
                                      u"port": test.destination_port},
                                     {u"local_ip": u"127.0.0.1"}))
    def test_successful_get_external_ip(self, logger):
        """
        A successful external IP lookup returns the local interface's IP.
        """
        class FakeSocket(object):
            def __init__(self, *args):
                self.addr = (b"0.0.0.0", 0)

            def getsockname(self):
                return self.addr

            def connect(self, addr):
                self.addr = (addr[0], 12345)
        self.patch(socket, "socket", FakeSocket())
        self.assertEqual(u"127.0.0.1",
                         _get_external_ip(u"localhost", self.destination_port))

    @capture_logging(lambda test, logger:
                     assertHasAction(test, logger, LOG_GET_EXTERNAL_IP, False,
                                     {u"host": u"localhost",
                                      u"port": test.destination_port},
                                     {u"exception":
                                      u"exceptions.RuntimeError"}))
    def test_failed_get_external_ip(self, logger):
        """
        A failed external IP lookup is retried (and the error logged).
        """
        original_connect = socket.socket.connect

        def fail_once(*args, **kwargs):
            socket.socket.connect = original_connect
            raise RuntimeError()
        self.patch(socket.socket, "connect", fail_once)
        self.assertEqual(u"127.0.0.1",
                         _get_external_ip(u"localhost", self.destination_port))
