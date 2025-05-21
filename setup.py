from setuptools import setup, find_packages

setup(
    name="esp32_micropython", # Changed from esp32_deploy_manager
    version="0.1.0", # You might want to increment this to 0.2.0 with the new feature
    py_modules=["dm"],
    python_requires=">=3.11,<3.12",
    install_requires=[
        "esptool==4.8.1",
        "mpremote==1.25.0",
        "micropython-esp32-stubs==1.25.0.post2",
        "micropython-esp32-esp32_generic_c3-stubs==1.23.0.post2",
        "pyserial>=3.5", # serial.tools.list_ports is part of pyserial
    ],
    entry_points={
        "console_scripts": [
            "esp32=dm:main", # The command-line tool remains 'esp32'
        ],
    },
    include_package_data=True,
)