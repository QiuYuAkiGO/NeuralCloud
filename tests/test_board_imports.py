"""A Linux writing surface does not import an independent mind."""
import subprocess
import sys


def test_body_import_loads_no_cognitive_or_memory_runtime():
    result = subprocess.run([sys.executable, "-c", """
import sys
import harness.board.service
assert not any(name.startswith(('harness.cognitive', 'harness.memory.service',
    'harness.pipeline', 'harness.runtime.supervisor')) for name in sys.modules)
"""], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_public_harness_exports_remain_compatible():
    import harness
    from harness.core.models import PersonaCore
    from harness.runtime.supervisor import RuntimeSupervisor
    assert harness.PersonaCore is PersonaCore
    assert harness.RuntimeSupervisor is RuntimeSupervisor
    assert set(harness.__all__) == set(harness._EXPORTS)
