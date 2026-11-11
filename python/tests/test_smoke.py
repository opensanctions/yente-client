def test_imports_yente_client() -> None:
    import yente_client

    assert yente_client is not None


def test_version_is_exported() -> None:
    import yente_client

    assert yente_client.__version__
    assert yente_client.__version__ in yente_client.Client(base_url="http://localhost").user_agent
