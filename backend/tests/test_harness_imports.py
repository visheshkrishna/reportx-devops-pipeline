from importlib import import_module


def test_local_tests_package_imports_factories_module() -> None:
    module = import_module("tests.factories")
    assert hasattr(module, "PersistenceFactory")
