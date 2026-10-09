REQUIREMENTS = {"testing": ["pytest", "pytest-cov", "numpy"], "install": ["numpy"]}

import setuptools

from pathlib import Path

version = {}
exec((Path(__file__).parent / "pagestore" / "version.py").read_text(), version)

setuptools.setup(
    name="pagestore",
    version=version["__version__"],
    description="Database of pages of data",
    author="Riccardo De Maria",
    author_email="riccardo.de.maria@cern.ch",
    url="https://github.com/rdemaria/pagestore",
    packages=setuptools.find_packages(),
    install_requires=REQUIREMENTS["install"],
    extras_require={"testing": REQUIREMENTS["testing"]},
    python_requires=">=3.10",
)
