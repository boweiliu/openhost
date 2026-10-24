import os


def app_data_dir(data_dir: str, app_name: str) -> str:
    """Host path of an app's persistent ``app_data`` directory, under ``Config.persistent_data_dir``."""
    return os.path.join(data_dir, "app_data", app_name)


def app_temp_data_dir(temp_data_dir: str, app_name: str) -> str:
    """Host path of an app's ``app_temp_data`` directory, under ``Config.temporary_data_dir``."""
    return os.path.join(temp_data_dir, "app_temp_data", app_name)
