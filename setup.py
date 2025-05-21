from setuptools import setup, find_packages
from pathlib import Path

# Read the contents of your README file
this_directory = Path(__file__).parent
long_description = (this_directory / "README.md").read_text(encoding="utf-8")

setup(
    name="esp32_micropython",
    version="0.2.0",  # Updated version
    py_modules=["dm"],
    python_requires=">=3.11,<3.12",
    install_requires=[
        "esptool>=4.8,<5.0",  
        "mpremote>=1.25,<2.0", 
        "pyserial>=3.5,<4.0",  
    ],
    entry_points={
        "console_scripts": [
            "esp32=dm:main",
        ],
    },
    author="aistack.pl",
    author_email="lael@aistack.pl",
    description="All-in-one utility for flashing and managing MicroPython deployments on ESP32-C3 SuperMini boards.",
    website="https://laelhalawani.github.io/esp32_micropython/",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/aistack-pl/esp32-micropython",
    license="MIT",
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "Topic :: Software Development :: Embedded Systems",
        "Topic :: System :: Hardware :: Hardware Drivers", 
        "Topic :: Terminals :: Serial",
        "Topic :: Utilities",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.11",
        "Operating System :: OS Independent",
        "Environment :: Console",
    ],
    keywords="esp32 esp32-c3 micropython flash deploy mpremote esptool firmware",
    project_urls={ 
        "Bug Reports": "https://github.com/aistack-pl/esp32-micropython/issues",
        "Source": "https://github.com/aistack-pl/esp32-micropython/",
        "Homepage": "https://laelhalawani.github.io/esp32_micropython/",
    },
    include_package_data=True,
)