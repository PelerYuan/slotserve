"""Container logs are bounded (R1-13): json-file, 50 MB x 5 files."""

import copy

from ..context import SessionContext
from ..harden import LOG_CONFIG
from ..model import ContainerSpec


class LoggingContributor:
    name = "logging"

    def contribute(self, ctx: SessionContext, spec: ContainerSpec) -> None:
        spec.log = copy.deepcopy(LOG_CONFIG)
