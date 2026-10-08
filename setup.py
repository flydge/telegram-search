"""Build source archives without local account metadata."""

import gzip
import os
import tarfile
import tempfile
from pathlib import Path

from setuptools import setup
from setuptools.command.sdist import sdist


class PublicSourceDistribution(sdist):
    def make_archive(self, base_name, format, root_dir=None, base_dir=None, owner=None, group=None):
        if format != "gztar":
            raise ValueError("public source distributions require gztar")
        epoch = max(0, int(os.environ.get("SOURCE_DATE_EPOCH", "0")))
        destination = Path(base_name + ".tar.gz").absolute()
        if self.dry_run:
            return str(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".public-sdist-", dir=destination.parent) as directory:
            archive_name = super().make_archive(str(Path(directory) / "raw"), format, root_dir, base_dir, owner, group)
            temporary = Path(directory) / "normalized.tar.gz"
            with temporary.open("wb") as raw:
                with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=epoch) as compressed:
                    with tarfile.open(fileobj=compressed, mode="w|") as output:
                        with tarfile.open(archive_name, "r:gz") as source:
                            for member in source:
                                if not (member.isfile() or member.isdir()):
                                    raise ValueError("source archive contains a non-regular entry")
                                member.uid = member.gid = 0
                                member.uname = member.gname = ""
                                member.mtime = epoch
                                member.pax_headers = {k: v for k, v in member.pax_headers.items() if k == "path"}
                                if member.isfile():
                                    with source.extractfile(member) as content:
                                        output.addfile(member, content)
                                else:
                                    output.addfile(member)
            os.replace(temporary, destination)
        return str(destination)


setup(cmdclass={"sdist": PublicSourceDistribution})
