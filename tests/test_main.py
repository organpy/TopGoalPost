"""Startup cog selection."""

def test_dev_cog_only_in_dev_mode():
    from main import DEV_COGS, select_cogs

    assert not set(DEV_COGS) & set(select_cogs({}))
    assert set(DEV_COGS) <= set(select_cogs({"ENV": "dev"}))
    assert set(DEV_COGS) <= set(select_cogs({"LOAD_DEV_COGS": "1"}))
