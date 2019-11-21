REQUIREMENTS = {"testing": ["pytest", "pytest-cov", "numpy"], "install": ["numpy"]}

import setuptools

import pagestore.version

setuptools.setup(
    name="pagestore",
    version=pagestore.version.__version__,
    description="Database of pages of data",
    author="Riccardo De Maria",
    author_email="riccardo.de.maria@cern.ch",
    url="https://github.com/rdemaria/pagestore",
    packages=["pagestore"],
    package_dir={"pagestore": "pagestore"},
    install_requires=REQUIREMENTS["install"],
)
