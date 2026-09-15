"""Public subsystem exports without eager construction-root imports."""

from importlib import import_module

_EXPORTS = {
    "ActionBackend": ("harness.runtime.action", "ActionBackend"),
    "ActionBackendResult": ("harness.runtime.models", "ActionBackendResult"),
    "ActionCoordinator": ("harness.runtime.action", "ActionCoordinator"),
    "ActionExecution": ("harness.runtime.models", "ActionExecution"),
    "ActionExecutionState": ("harness.runtime.models", "ActionExecutionState"),
    "AgencyState": ("harness.runtime.models", "AgencyState"),
    "CheckpointStore": ("harness.runtime.checkpoint", "CheckpointStore"),
    "BodyStateProvider": ("harness.embodiment.ports", "BodyStateProvider"),
    "CapabilityProvider": ("harness.embodiment.ports", "CapabilityProvider"),
    "CognitiveServiceClient": ("harness.runtime.adapters", "CognitiveServiceClient"),
    "ControlAuthorizationError": ("harness.runtime.control", "ControlAuthorizationError"),
    "ControlPermission": ("harness.runtime.control", "ControlPermission"),
    "ControlPlane": ("harness.runtime.control", "ControlPlane"),
    "DeliveryBackend": ("harness.runtime.action", "DeliveryBackend"),
    "DeliveryResult": ("harness.runtime.models", "DeliveryResult"),
    "DomainCommandError": ("harness.runtime.supervisor", "DomainCommandError"),
    "DomainRpcError": ("harness.runtime.rpc", "DomainRpcError"),
    "DomainSnapshot": ("harness.runtime.adapters", "DomainSnapshot"),
    "EventDispatchResult": ("harness.runtime.models", "EventDispatchResult"),
    "HomeostasisServiceClient": ("harness.runtime.adapters", "HomeostasisServiceClient"),
    "HttpCognitiveClient": ("harness.runtime.rpc", "HttpCognitiveClient"),
    "HttpHomeostasisClient": ("harness.runtime.rpc", "HttpHomeostasisClient"),
    "InMemoryDeliveryBackend": ("harness.runtime.action", "InMemoryDeliveryBackend"),
    "JournalCorruptionError": ("harness.runtime.journal", "JournalCorruptionError"),
    "JournalUnavailableError": ("harness.runtime.journal", "JournalUnavailableError"),
    "LateDomainResultError": ("harness.runtime.supervisor", "LateDomainResultError"),
    "LifecycleTransition": ("harness.runtime.models", "LifecycleTransition"),
    "MonitorEvent": ("harness.runtime.monitor", "MonitorEvent"),
    "MonitorRecorder": ("harness.runtime.monitor", "MonitorRecorder"),
    "MonitorRun": ("harness.runtime.monitor", "MonitorRun"),
    "MonitorSample": ("harness.runtime.monitor", "MonitorSample"),
    "MonitorStore": ("harness.runtime.monitor", "MonitorStore"),
    "NoopActionBackend": ("harness.runtime.action", "NoopActionBackend"),
    "PassiveHomeostasisAdapter": ("harness.runtime.adapters", "PassiveHomeostasisAdapter"),
    "PauseObservationBuffer": ("harness.runtime.perception", "PauseObservationBuffer"),
    "PerceptionGuard": ("harness.runtime.perception", "PerceptionGuard"),
    "PerceptionBackend": ("harness.embodiment.ports", "PerceptionBackend"),
    "PipelineCognitiveAdapter": ("harness.runtime.adapters", "PipelineCognitiveAdapter"),
    "PriorityEventQueue": ("harness.runtime.queue", "PriorityEventQueue"),
    "QueueStatistics": ("harness.runtime.models", "QueueStatistics"),
    "QueueCapacityError": ("harness.runtime.queue", "QueueCapacityError"),
    "RealTimeScheduler": ("harness.runtime.scheduler", "RealTimeScheduler"),
    "ResumeObservation": ("harness.runtime.models", "ResumeObservation"),
    "RuntimeCheckpoint": ("harness.runtime.checkpoint", "RuntimeCheckpoint"),
    "RuntimeEvent": ("harness.runtime.models", "RuntimeEvent"),
    "RuntimeEventPriority": ("harness.runtime.models", "RuntimeEventPriority"),
    "RuntimeEventStream": ("harness.runtime.control", "RuntimeEventStream"),
    "RuntimeEventType": ("harness.runtime.models", "RuntimeEventType"),
    "RuntimeHealth": ("harness.runtime.models", "RuntimeHealth"),
    "RuntimeIngress": ("harness.runtime.supervisor", "RuntimeIngress"),
    "RuntimeJournal": ("harness.runtime.journal", "RuntimeJournal"),
    "RuntimeJournalRecord": ("harness.runtime.journal", "RuntimeJournalRecord"),
    "RuntimeLifecycle": ("harness.runtime.models", "RuntimeLifecycle"),
    "RuntimeStateError": ("harness.runtime.supervisor", "RuntimeStateError"),
    "RuntimeStatus": ("harness.runtime.models", "RuntimeStatus"),
    "RuntimeSupervisor": ("harness.runtime.supervisor", "RuntimeSupervisor"),
    "RuntimeWatchdog": ("harness.runtime.watchdog", "RuntimeWatchdog"),
    "ServiceHealth": ("harness.runtime.adapters", "ServiceHealth"),
    "WatchdogAction": ("harness.runtime.models", "WatchdogAction"),
    "WatchdogDecision": ("harness.runtime.models", "WatchdogDecision"),
    "UnknownDeliveryStateError": ("harness.runtime.supervisor", "UnknownDeliveryStateError"),
    "create_control_app": ("harness.runtime.control", "create_control_app"),
    "create_domain_service_app": ("harness.runtime.rpc", "create_domain_service_app"),
}

__all__ = list(_EXPORTS)


def __getattr__(name):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(target[0]), target[1])
    globals()[name] = value
    return value
