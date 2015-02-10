# Copyright Hybrid Logic Ltd.  See LICENSE file for details.

"""
Tests for ``flocker.node._loop``.
"""

from zope.interface import implementer

from twisted.trial.unittest import SynchronousTestCase
from twisted.test.proto_helpers import StringTransport, MemoryReactorClock
from twisted.internet.protocol import Protocol, ReconnectingClientFactory
from twisted.internet.defer import succeed, Deferred

from ...testtools import FakeAMPClient
from .._loop import (
    build_cluster_status_fsm, ClusterStatusInputs, _ClientStatusUpdate,
    _StatusUpdate, _ConnectedToControlService, ConvergenceLoopInputs,
    ConvergenceLoopStates, build_convergence_loop_fsm, AgentLoopService,
    ClusterStatus, ConvergenceLoop,
    )
from .._deploy import IDeployer, IStateChange
from ...control._protocol import NodeStateCommand, _AgentLocator, AgentAMP
from ...control.test.test_protocol import iconvergence_agent_tests_factory


def build_protocol():
    """
    :return: ``Protocol`` hooked up to transport.
    """
    p = Protocol()
    p.makeConnection(StringTransport())
    return p


class StubFSM(object):
    """
    A finite state machine look-alike that just records inputs.
    """
    def __init__(self):
        self.inputted = []

    def receive(self, symbol):
        self.inputted.append(symbol)


class ClusterStatusFSMTests(SynchronousTestCase):
    """
    Tests for the cluster status FSM.
    """
    def setUp(self):
        self.convergence_loop = StubFSM()
        self.fsm = build_cluster_status_fsm(self.convergence_loop)

    def assertConvergenceLoopInputted(self, expected):
        """
        Assert that that given set of symbols were input to the agent
        operation FSM.
        """
        self.assertEqual(self.convergence_loop.inputted, expected)

    def test_creation_no_side_effects(self):
        """
        Creating the FSM has no side effects.
        """
        self.assertConvergenceLoopInputted([])

    def test_first_status_update(self):
        """
        Once the client has been connected and a status update received it
        notifies the convergence loop FSM of this.
        """
        client = object()
        desired = object()
        state = object()
        self.fsm.receive(_ConnectedToControlService(client=client))
        self.fsm.receive(_StatusUpdate(configuration=desired, state=state))
        self.assertConvergenceLoopInputted(
            [_ClientStatusUpdate(client=client, configuration=desired,
                                 state=state)])

    def test_second_status_update(self):
        """
        Further status updates are also passed to the convergence loop FSM.
        """
        client = object()
        desired1 = object()
        state1 = object()
        desired2 = object()
        state2 = object()
        self.fsm.receive(_ConnectedToControlService(client=client))
        # Initially some other status:
        self.fsm.receive(_StatusUpdate(configuration=desired1, state=state1))
        self.fsm.receive(_StatusUpdate(configuration=desired2, state=state2))
        self.assertConvergenceLoopInputted(
            [_ClientStatusUpdate(client=client, configuration=desired1,
                                 state=state1),
             _ClientStatusUpdate(client=client, configuration=desired2,
                                 state=state2)])

    def test_status_update_no_disconnect(self):
        """
        Neither new connections nor status updates cause the client to be
        disconnected.
        """
        client = build_protocol()
        self.fsm.receive(_ConnectedToControlService(client=client))
        self.fsm.receive(_StatusUpdate(configuration=object(),
                                       state=object()))
        self.assertFalse(client.transport.disconnecting)

    def test_disconnect_before_status_update(self):
        """
        If the client disconnects before a status update is received then no
        notification is needed for convergence loop FSM.
        """
        self.fsm.receive(_ConnectedToControlService(client=build_protocol()))
        self.fsm.receive(ClusterStatusInputs.DISCONNECTED_FROM_CONTROL_SERVICE)
        self.assertConvergenceLoopInputted([])

    def test_disconnect_after_status_update(self):
        """
        If the client disconnects after a status update is received then the
        convergence loop FSM is notified.
        """
        client = object()
        desired = object()
        state = object()
        self.fsm.receive(_ConnectedToControlService(client=client))
        self.fsm.receive(_StatusUpdate(configuration=desired, state=state))
        self.fsm.receive(ClusterStatusInputs.DISCONNECTED_FROM_CONTROL_SERVICE)
        self.assertConvergenceLoopInputted(
            [_ClientStatusUpdate(client=client, configuration=desired,
                                 state=state),
             ConvergenceLoopInputs.STOP])

    def test_status_update_after_reconnect(self):
        """
        If the client disconnects, reconnects, and a new status update is
        received then the convergence loop FSM is notified.
        """
        client = object()
        desired = object()
        state = object()
        self.fsm.receive(_ConnectedToControlService(client=client))
        self.fsm.receive(_StatusUpdate(configuration=desired, state=state))
        self.fsm.receive(ClusterStatusInputs.DISCONNECTED_FROM_CONTROL_SERVICE)
        client2 = object()
        desired2 = object()
        state2 = object()
        self.fsm.receive(_ConnectedToControlService(client=client2))
        self.fsm.receive(_StatusUpdate(configuration=desired2, state=state2))
        self.assertConvergenceLoopInputted(
            [_ClientStatusUpdate(client=client, configuration=desired,
                                 state=state),
             ConvergenceLoopInputs.STOP,
             _ClientStatusUpdate(client=client2, configuration=desired2,
                                 state=state2)])

    def test_shutdown_before_connect(self):
        """
        If the FSM is shutdown before a connection is made nothing happens.
        """
        self.fsm.receive(ClusterStatusInputs.SHUTDOWN)
        self.assertConvergenceLoopInputted([])

    def test_shutdown_after_connect(self):
        """
        If the FSM is shutdown after connection but before status update is
        received then it disconnects but does not notify the agent
        operation FSM.
        """
        client = build_protocol()
        self.fsm.receive(_ConnectedToControlService(client=client))
        self.fsm.receive(ClusterStatusInputs.SHUTDOWN)
        self.assertEqual((client.transport.disconnecting,
                          self.convergence_loop.inputted),
                         (True, []))

    def test_shutdown_after_status_update(self):
        """
        If the FSM is shutdown after connection and status update is received
        then it disconnects and also notifys the convergence loop FSM that
        is should stop.
        """
        client = build_protocol()
        desired = object()
        state = object()
        self.fsm.receive(_ConnectedToControlService(client=client))
        self.fsm.receive(_StatusUpdate(configuration=desired, state=state))
        self.fsm.receive(ClusterStatusInputs.SHUTDOWN)
        self.assertEqual((client.transport.disconnecting,
                          self.convergence_loop.inputted[-1]),
                         (True, ConvergenceLoopInputs.STOP))

    def test_shutdown_fsm_ignores_disconnection(self):
        """
        If the FSM has been shutdown it ignores disconnection event.
        """
        client = build_protocol()
        desired = object()
        state = object()
        self.fsm.receive(_ConnectedToControlService(client=client))
        self.fsm.receive(_StatusUpdate(configuration=desired, state=state))
        self.fsm.receive(ClusterStatusInputs.SHUTDOWN)
        self.fsm.receive(ClusterStatusInputs.DISCONNECTED_FROM_CONTROL_SERVICE)
        self.assertConvergenceLoopInputted([
            _ClientStatusUpdate(client=client, configuration=desired,
                                state=state),
            # This is caused by the shutdown... and the disconnect results
            # in no further messages:
            ConvergenceLoopInputs.STOP])

    def test_shutdown_fsm_ignores_cluster_status(self):
        """
        If the FSM has been shutdown it ignores cluster status update.
        """
        client = build_protocol()
        desired = object()
        state = object()
        self.fsm.receive(_ConnectedToControlService(client=client))
        self.fsm.receive(ClusterStatusInputs.SHUTDOWN)
        self.fsm.receive(_StatusUpdate(configuration=desired, state=state))
        # We never send anything to convergence loop FSM:
        self.assertConvergenceLoopInputted([])


@implementer(IStateChange)
class ControllableAction(object):
    """
    ``IStateChange`` whose results can be controlled.
    """
    def __init__(self, result):
        self.result = result
        self.called = False
        self.deployer = None

    def run(self, deployer):
        self.called = True
        self.deployer = deployer
        return self.result


@implementer(IDeployer)
class ControllableDeployer(object):
    """
    ``IDeployer`` whose results can be controlled.
    """
    def __init__(self, local_states, calculated_actions):
        self.local_states = local_states
        self.calculated_actions = calculated_actions
        self.calculate_inputs = []

    def discover_local_state(self):
        return self.local_states.pop(0)

    def calculate_necessary_state_changes(self, local_state,
                                          desired_configuration,
                                          cluster_state):
        self.calculate_inputs.append(
            (local_state, desired_configuration, cluster_state))
        return self.calculated_actions.pop(0)


class ConvergenceLoopFSMTests(SynchronousTestCase):
    """
    Tests for FSM created by ``build_convergence_loop_fsm``.
    """
    def test_new_stopped(self):
        """
        A newly created FSM is stopped.
        """
        loop = build_convergence_loop_fsm(ControllableDeployer([], []))
        self.assertEqual(loop.state, ConvergenceLoopStates.STOPPED)

    def test_new_status_update_starts_discovery(self):
        """
        A stopped FSM that receives a status update starts discovery.
        """
        deployer = ControllableDeployer([Deferred()], [])
        loop = build_convergence_loop_fsm(deployer)
        loop.receive(_ClientStatusUpdate(client=object(),
                                         configuration=object(),
                                         state=object()))
        self.assertEqual(len(deployer.local_states), 0)  # Discovery started

    def successful_amp_client(self, local_states):
        """
        Create AMP client that can respond successfully to a
        ``NodeStateCommand``.

        :param local_states: The node states we expect to be able to send.

        :return FakeAMPClient: Fake AMP client appropriately setup.
        """
        client = FakeAMPClient()
        for local_state in local_states:
            client.register_response(
                NodeStateCommand, dict(node_state=local_state),
                {"result": None})
        return client

    def test_convergence_done_notify(self):
        """
        A FSM doing convergence that gets a discovery result send the
        discovered state to the control service using the last received
        client.
        """
        local_state = object()
        client = self.successful_amp_client([local_state])
        action = ControllableAction(Deferred())
        deployer = ControllableDeployer([succeed(local_state)], [action])
        loop = build_convergence_loop_fsm(deployer)
        loop.receive(_ClientStatusUpdate(client=client,
                                         configuration=object(),
                                         state=object()))
        self.assertEqual(client.calls, [(NodeStateCommand,
                                         dict(node_state=local_state))])

    def test_convergence_done_changes(self):
        """
        A FSM doing convergence that gets a discovery result starts applying
        calculated changes using last received desired configuration and
        cluster state.
        """
        local_state = object()
        configuration = object()
        state = object()
        # Since this Deferred is unfired we never proceed to next
        # iteration; if we did we'd get exception from discovery since we
        # only configured one discovery result.
        action = ControllableAction(Deferred())
        deployer = ControllableDeployer([succeed(local_state)], [action])
        loop = build_convergence_loop_fsm(deployer)
        loop.receive(_ClientStatusUpdate(
            client=self.successful_amp_client([local_state]),
            configuration=configuration, state=state))
        # Calculating actions happened, and result was run:
        self.assertEqual((deployer.calculate_inputs, action.called),
                         ([(local_state, configuration, state)], True))

    def test_convergence_done_start_new_iteration(self):
        """
        A FSM doing a convergence iteration does another iteration when
        applying changes is done.
        """
        local_state = object()
        local_state2 = object()
        configuration = object()
        state = object()
        action = ControllableAction(succeed(None))
        # Because the second action result is unfired Deferred, the second
        # iteration will never finish; applying its changes waits for this
        # Deferred to fire.
        action2 = ControllableAction(Deferred())
        deployer = ControllableDeployer(
            [succeed(local_state), succeed(local_state2)],
            [action, action2])
        client = self.successful_amp_client([local_state, local_state2])
        loop = build_convergence_loop_fsm(deployer)
        loop.receive(_ClientStatusUpdate(
            client=client, configuration=configuration, state=state))
        # Calculating actions happened, result was run... and then we did
        # whole thing again:
        self.assertEqual((deployer.calculate_inputs, client.calls),
                         ([(local_state, configuration, state),
                           (local_state2, configuration, state)],
                          [(NodeStateCommand, dict(node_state=local_state)),
                           (NodeStateCommand, dict(node_state=local_state2))]))

    def test_convergence_status_update(self):
        """
        A FSM doing convergence that receives a status update stores the
        client, desired configuration and cluster state, which are then
        used in next convergence iteration.
        """
        local_state = object()
        local_state2 = object()
        configuration = object()
        state = object()
        # Until this Deferred fires the first iteration won't finish:
        action = ControllableAction(Deferred())
        # Until this Deferred fires the second iteration won't finish:
        action2 = ControllableAction(Deferred())
        deployer = ControllableDeployer(
            [succeed(local_state), succeed(local_state2)],
            [action, action2])
        client = self.successful_amp_client([local_state])
        loop = build_convergence_loop_fsm(deployer)
        loop.receive(_ClientStatusUpdate(
            client=client, configuration=configuration, state=state))

        # Calculating actions happened, action is run, but waits for
        # Deferred to be fired... Meanwhile a new status update appears!
        client2 = self.successful_amp_client([local_state2])
        configuration2 = object()
        state2 = object()
        loop.receive(_ClientStatusUpdate(
            client=client2, configuration=configuration2, state=state2))
        # Action finally finishes, and we can move on to next iteration,
        # which happens with second set of client, desired configuration
        # and cluster state:
        action.result.callback(None)
        self.assertEqual(
            (deployer.calculate_inputs, client.calls, client2.calls),
            ([(local_state, configuration, state),
              (local_state2, configuration2, state2)],
             [(NodeStateCommand, dict(node_state=local_state))],
             [(NodeStateCommand, dict(node_state=local_state2))]))

    def test_convergence_stop(self):
        """
        A FSM doing convergence that receives a stop input stops when the
        convergence iteration finishes.
        """
        local_state = object()
        configuration = object()
        state = object()
        # Until this Deferred fires the first iteration won't finish:
        action = ControllableAction(Deferred())
        # Only one discovery result is configured, so a second attempt at
        # discovery would fail:
        deployer = ControllableDeployer([succeed(local_state)],
                                        [action])
        client = self.successful_amp_client([local_state])
        loop = build_convergence_loop_fsm(deployer)
        loop.receive(_ClientStatusUpdate(
            client=client, configuration=configuration, state=state))

        # Calculating actions happened, action is run, but waits for
        # Deferred to be fired... Meanwhile a stop input is received!
        loop.receive(ConvergenceLoopInputs.STOP)
        # Action finally finishes, and nothing more happens; if another
        # iteration did happen the loop would attempt discovery, which
        # would make deployer.discover_local_state() to fail since it was
        # only configured with one result.
        action.result.callback(None)
        self.assertEqual(
            (deployer.calculate_inputs, client.calls, loop.state),
            ([(local_state, configuration, state)],
             [(NodeStateCommand, dict(node_state=local_state))],
             ConvergenceLoopStates.STOPPED))

    def test_convergence_stop_then_status_update(self):
        """
        A FSM doing convergence that receives a stop input and then a status
        update continues on to to next convergence iteration (i.e. stop
        ends up being ignored).
        """
        local_state = object()
        local_state2 = object()
        configuration = object()
        state = object()
        # Until this Deferred fires the first iteration won't finish:
        action = ControllableAction(Deferred())
        # Until this Deferred fires the second iteration won't finish:
        action2 = ControllableAction(Deferred())
        deployer = ControllableDeployer(
            [succeed(local_state), succeed(local_state2)],
            [action, action2])
        client = self.successful_amp_client([local_state])
        loop = build_convergence_loop_fsm(deployer)
        loop.receive(_ClientStatusUpdate(
            client=client, configuration=configuration, state=state))

        # Calculating actions happened, action is run, but waits for
        # Deferred to be fired... Meanwhile a new status update appears!
        client2 = self.successful_amp_client([local_state2])
        configuration2 = object()
        state2 = object()
        loop.receive(ConvergenceLoopInputs.STOP)
        # And then another status update!
        loop.receive(_ClientStatusUpdate(
            client=client2, configuration=configuration2, state=state2))
        # Action finally finishes, and we can move on to next iteration,
        # which happens with second set of client, desired configuration
        # and cluster state:
        action.result.callback(None)
        self.assertEqual(
            (deployer.calculate_inputs, client.calls, client2.calls),
            ([(local_state, configuration, state),
              (local_state2, configuration2, state2)],
             [(NodeStateCommand, dict(node_state=local_state))],
             [(NodeStateCommand, dict(node_state=local_state2))]))


class AgentLoopServiceTests(SynchronousTestCase):
    """
    Tests for ``AgentLoopService``.
    """
    def test_initialization(self):
        """
        A newly created service has a cluster status FSM pointing at a
        convergence loop FSM configured with the given deployer.
        """
        deployer = object()
        service = AgentLoopService(
            reactor=None, deployer=deployer, host=u"example.com", port=1234)
        cluster_status_fsm_world = service.cluster_status._fsm._world.original
        convergence_loop_fsm_world = (
            cluster_status_fsm_world.convergence_loop_fsm._fsm._world.original)
        self.assertEqual((cluster_status_fsm_world.__class__,
                          convergence_loop_fsm_world.__class__,
                          convergence_loop_fsm_world.deployer),
                         (ClusterStatus, ConvergenceLoop, deployer))

    def test_start_service(self):
        """
        Starting the service starts a reconnecting TCP client to given host
        and port which calls ``build_agent_client`` with the service when
        connected.
        """
        reactor = MemoryReactorClock()
        service = AgentLoopService(
            reactor=reactor, deployer=object(), host=u"example.com", port=1234)
        service.startService()
        host, port, factory = reactor.tcpClients[0][:3]
        protocol = factory.buildProtocol(None)
        self.assertEqual((host, port, factory.__class__,
                          factory.continueTrying,
                          protocol.__class__, protocol.locator,
                          service.running),
                         (u"example.com", 1234, ReconnectingClientFactory,
                          True, AgentAMP, _AgentLocator(service), True))

    def test_stop_service(self):
        """
        Stopping the service stops the reconnecting TCP client and inputs
        shutdown event to the cluster status FSM.
        """
        reactor = MemoryReactorClock()
        service = AgentLoopService(
            reactor=reactor, deployer=object(), host=u"example.com", port=1234)
        service.cluster_status = fsm = StubFSM()
        service.startService()
        service.stopService()
        self.assertEqual((service.factory.continueTrying, fsm.inputted,
                          service.running),
                         (False, [ClusterStatusInputs.SHUTDOWN], False))

    def test_connected(self):
        """
        When ``connnected()`` is called a ``_ConnectedToControlService`` input
        is passed to the cluster status FSM.
        """
        service = AgentLoopService(
            reactor=None, deployer=object(), host=u"example.com", port=1234)
        service.cluster_status = fsm = StubFSM()
        client = object()
        service.connected(client)
        self.assertEqual(fsm.inputted,
                         [_ConnectedToControlService(client=client)])

    def test_disconnected(self):
        """
        When ``connnected()`` is called a
        ``ClusterStatusInputs.DISCONNECTED_FROM_CONTROL_SERVICE`` input is
        passed to the cluster status FSM.
        """
        service = AgentLoopService(
            reactor=None, deployer=object(), host=u"example.com", port=1234)
        service.cluster_status = fsm = StubFSM()
        service.disconnected()
        self.assertEqual(
            fsm.inputted,
            [ClusterStatusInputs.DISCONNECTED_FROM_CONTROL_SERVICE])

    def test_cluster_updated(self):
        """
        When ``cluster_updated()`` is called a ``_StatusUpdate`` input is
        passed to the cluster status FSM.
        """
        service = AgentLoopService(
            reactor=None, deployer=object(), host=u"example.com", port=1234)
        service.cluster_status = fsm = StubFSM()
        config = object()
        state = object()
        service.cluster_updated(config, state)
        self.assertEqual(fsm.inputted, [_StatusUpdate(configuration=config,
                                                      state=state)])


def _build_service(test):
    """
    Fixture for creating ``AgentLoopService``.
    """
    service = AgentLoopService(
        reactor=None, deployer=object(), host=u"example.com", port=1234)
    service.cluster_status = StubFSM()
    return service


class AgentLoopServiceInterfaceTests(
        iconvergence_agent_tests_factory(_build_service)):
    """
    ``IConvergenceAgent`` tests for ``AgentLoopService``.
    """
