from app.config import Settings


def test_the_showcase_service_is_off_by_default_and_read_from_settings():
    assert Settings(_env_file=None).showcase_service == ""
    assert Settings(_env_file=None, showcase_service="Gobblr").showcase_service == "Gobblr"
