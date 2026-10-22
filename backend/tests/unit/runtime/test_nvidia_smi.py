"""Parsing ``nvidia-smi --query-gpu=... --format=csv,noheader,nounits`` (tests/samples/nvidia-smi/).

Samples are named ``<os>-<gpu>-<driver>.csv``. windows-rtx4060-laptop-591.44 was captured on real hardware (CRLF
line endings kept, see the folder's .gitattributes); the others reproduce the output format of those drivers
(multi-GPU, MIG "[N/A]", "[Not Supported]", a lost GPU, no devices) and should be replaced by captures from
real machines as the sample library grows (tools/verify).
"""

import pytest

from slotserve.runtime.gpu import _QUERY, parse_query_output

# sample -> [(index, name, memory_used_mb, memory_total_mb, utilization_pct, temperature_c, power_w), ...]
EXPECTED = {
    "windows-rtx4060-laptop-591.44.csv": [(0, "NVIDIA GeForce RTX 4060 Laptop GPU", 1553, 8188, 13, 61, 5.78)],
    "linux-a100-sxm4-80gb-x8-535.104.05.csv": [
        (0, "NVIDIA A100-SXM4-80GB", 71234, 81920, 100, 64, 348.52),
        (1, "NVIDIA A100-SXM4-80GB", 71234, 81920, 98, 66, 361.07),
        (2, "NVIDIA A100-SXM4-80GB", 4, 81920, 0, 33, 61.44),
        (3, "NVIDIA A100-SXM4-80GB", 4, 81920, 0, 32, 59.80),
        (4, "NVIDIA A100-SXM4-80GB", 40112, 81920, 37, 51, 172.93),
        (5, "NVIDIA A100-SXM4-80GB", 40112, 81920, 41, 52, 180.10),
        (6, "NVIDIA A100-SXM4-80GB", 0, 81920, 0, 30, 58.21),
        (7, "NVIDIA A100-SXM4-80GB", 0, 81920, 0, 31, 60.02),
    ],
    # MIG mode: utilization.gpu is "[N/A]" for the partitioned GPU.
    "linux-h100-mig-550.54.15.csv": [
        (0, "NVIDIA H100 80GB HBM3", 25, 81559, 0, 36, 72.18),
        (1, "NVIDIA H100 80GB HBM3", 61344, 81559, 87, 58, 512.66),
    ],
    # Older GeForce mobile drivers: no power reading.
    "linux-gtx1050-mobile-418.56.csv": [(0, "GeForce GTX 1050", 1342, 4040, 23, 47, None)],
    # GPU 1 fell off the bus: an error line instead of its row, the others are still reported.
    "linux-rtx3090-x4-gpu-lost-525.85.12.csv": [
        (0, "NVIDIA GeForce RTX 3090", 23816, 24576, 99, 78, 347.21),
        (2, "NVIDIA GeForce RTX 3090", 1, 24576, 0, 29, 21.50),
        (3, "NVIDIA GeForce RTX 3090", 12044, 24576, 45, 61, 198.04),
    ],
    "linux-no-devices.csv": [],
}


def as_tuples(gpus):
    return [
        (g.index, g.name, g.memory_used_mb, g.memory_total_mb, g.utilization_pct, g.temperature_c, g.power_w)
        for g in gpus
    ]


def test_every_sample_has_expectations(samples_dir):
    assert sorted(p.name for p in (samples_dir / "nvidia-smi").glob("*.csv")) == sorted(EXPECTED)


@pytest.mark.parametrize("sample", sorted(EXPECTED))
def test_parse_sample(samples_dir, sample):
    raw = (samples_dir / "nvidia-smi" / sample).read_bytes().decode()  # keeps CRLF as captured
    assert as_tuples(parse_query_output(raw)) == EXPECTED[sample]


def test_query_fields_match_the_parser_order():
    assert _QUERY.split(",") == [
        "index",
        "name",
        "memory.used",
        "memory.total",
        "utilization.gpu",
        "temperature.gpu",
        "power.draw",
    ]


def test_short_and_empty_output():
    assert parse_query_output("") == []
    assert parse_query_output("0, Some GPU, 1, 2\n") == []
