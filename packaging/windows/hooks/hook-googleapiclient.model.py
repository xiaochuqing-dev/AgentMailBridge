"""Limit packaged Google discovery data to the Gmail API used by the product."""

from PyInstaller.utils.hooks import collect_data_files, copy_metadata


datas = copy_metadata("google_api_python_client")
datas += collect_data_files(
    "googleapiclient.discovery_cache",
    includes=["documents/gmail.v1.json"],
)
