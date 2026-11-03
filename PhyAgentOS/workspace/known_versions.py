"""Known released template hashes used to recognise unmodified old installs.

The runtime must not depend on git, so release hashes are recorded here. Only
unique contents are listed. A workspace file whose hash appears below is an
untouched original from that release and may be upgraded. Anything else is
treated as user-customised and is never overwritten.
"""

from __future__ import annotations

# path -> {sha256: first release known to contain that content}
KNOWN_TEMPLATE_HASHES: dict[str, dict[str, str]] = {
    "AGENTS.md": {
        "df3932f3e9d41efdae71150f48548982970d1b8b5fbfc41789c6927cf8467366": "v0.1.6",
    },
    "EMBODIED.md": {
        "41c258a5738fe4b348f64281a96ee879327ee4d4f45c2f60fc155fdba16ed3ce": "v0.1.6",
        "d215acef7d2789ae228059740abab937598d9e397671e4715b83cf716c053bc9": "v0.2.0",
        "55e1327eef71cef9e6ed95c6188368b8da96e1fa69366726a58a3d18ecdbb055": "v1.0.0",
    },
    "ENVIRONMENT.md": {
        "aad24af73702e232396c86f9a177f2b9dc53a0b59c6bf0d5615217485b8828f9": "v0.1.6",
        "c3e3c492075d8fe929009fbfbba6ee2626875bccbd96a367b787c87374b8593b": "v0.2.0",
        "b3259eb307ce3c86b414c42c14a5898fb962dfb76c96cb46a01f9246f9856a7f": "v1.0.0",
    },
    "HEARTBEAT.md": {
        "40c438e1984d2a5612d28c9d5b27e17ff534ecac333cd37c855411bcc44bdea3": "v0.1.6",
        "1aee34cc251d3395733472543a891cf7103ca196198b829068811ce90a8dd9a5": "v0.2.0",
        "630ccb1f901ea5979fcba59f933eb728b8ebd0e15cd1dbb48730a23874fe8a0a": "v1.0.0",
    },
    "LESSONS.md": {
        "a782888e082a5464a73105f617bfcaecbd2d8ec58df81c6bbdb081dd541542f9": "v0.1.6",
        "031576beed54c63944f20d4d62cdff9781c792dd1483a5aaa50e3045a04162ea": "v1.0.0",
    },
    "ORCHESTRATOR.md": {
        "d62c18c5b291f181dfd5ea1137b5cc71b3032b0a58005d29dcb729c6babbd434": "v0.1.6",
    },
    "SKILLS.md": {
        "ce9f20c973093c04ada1ee554b17553517406b33730a3969ed93306bf6bbfd9b": "v0.1.6",
        "1368bc2f5727cf8c9549a27603bd5331ed65ab431da23a6d7f10e29d7ab23313": "v0.2.0",
        "75424a959b3f75703c223a81ca51f5423b6e8bf6f3cc94b97e5697807ac6b39f": "v1.0.0",
    },
    "SOUL.md": {
        "53055a370ce7ee409a61c5f23382d2bd0b3207fc17de734f37ce86e1f8e7591c": "v0.1.6",
    },
    "TASK.md": {
        "b4c5b741a347c64990360195273d3f0be7e46275ab674b95fbc7be6b8c934401": "v0.1.6",
    },
    "TOOLS.md": {
        "f32c144155cb564e72c766f9acd1e27c4ee7a7dd8311dbe7cf23533047c41476": "v0.1.6",
    },
    "USER.md": {
        "31afb46b738ecabb995ce3ec88cba46112c369a49bfaece74f6405acda6ad161": "v0.1.6",
    },
    "memory/MEMORY.md": {
        "890d87460fc585a6f44b3db7087cb13e888fcb2582d30cbc1aae2d2b60dbe0a1": "v0.1.6",
    },
}

# (path, sha256) -> (release tag, commit). The test suite verifies these with
# real ``git show <commit>:PhyAgentOS/templates/<path>``; runtime never uses git.
KNOWN_TEMPLATE_PROVENANCE: dict[tuple[str, str], tuple[str, str]] = {
    ("AGENTS.md", "df3932f3e9d41efdae71150f48548982970d1b8b5fbfc41789c6927cf8467366"): (
        "v0.1.6.20260722-e68f399",
        "e68f399b3536f3f3bc42b9c46b00ecb97bd75193",
    ),
    ("EMBODIED.md", "41c258a5738fe4b348f64281a96ee879327ee4d4f45c2f60fc155fdba16ed3ce"): (
        "v0.1.6.20260722-e68f399",
        "e68f399b3536f3f3bc42b9c46b00ecb97bd75193",
    ),
    ("EMBODIED.md", "d215acef7d2789ae228059740abab937598d9e397671e4715b83cf716c053bc9"): (
        "v0.2.0.20260811-7ee45c0",
        "7ee45c0e6922c3198feacd78d0d37d028bc56240",
    ),
    ("EMBODIED.md", "55e1327eef71cef9e6ed95c6188368b8da96e1fa69366726a58a3d18ecdbb055"): (
        "v1.0.0",
        "c6736e651cf0621e4ffed3397e0c5cc820051f50",
    ),
    ("ENVIRONMENT.md", "aad24af73702e232396c86f9a177f2b9dc53a0b59c6bf0d5615217485b8828f9"): (
        "v0.1.6.20260722-e68f399",
        "e68f399b3536f3f3bc42b9c46b00ecb97bd75193",
    ),
    ("ENVIRONMENT.md", "c3e3c492075d8fe929009fbfbba6ee2626875bccbd96a367b787c87374b8593b"): (
        "v0.2.0.20260811-7ee45c0",
        "7ee45c0e6922c3198feacd78d0d37d028bc56240",
    ),
    ("ENVIRONMENT.md", "b3259eb307ce3c86b414c42c14a5898fb962dfb76c96cb46a01f9246f9856a7f"): (
        "v1.0.0",
        "c6736e651cf0621e4ffed3397e0c5cc820051f50",
    ),
    ("HEARTBEAT.md", "40c438e1984d2a5612d28c9d5b27e17ff534ecac333cd37c855411bcc44bdea3"): (
        "v0.1.6.20260722-e68f399",
        "e68f399b3536f3f3bc42b9c46b00ecb97bd75193",
    ),
    ("HEARTBEAT.md", "1aee34cc251d3395733472543a891cf7103ca196198b829068811ce90a8dd9a5"): (
        "v0.2.0.20260811-7ee45c0",
        "7ee45c0e6922c3198feacd78d0d37d028bc56240",
    ),
    ("HEARTBEAT.md", "630ccb1f901ea5979fcba59f933eb728b8ebd0e15cd1dbb48730a23874fe8a0a"): (
        "v1.0.0",
        "c6736e651cf0621e4ffed3397e0c5cc820051f50",
    ),
    ("LESSONS.md", "a782888e082a5464a73105f617bfcaecbd2d8ec58df81c6bbdb081dd541542f9"): (
        "v0.1.6.20260722-e68f399",
        "e68f399b3536f3f3bc42b9c46b00ecb97bd75193",
    ),
    ("LESSONS.md", "031576beed54c63944f20d4d62cdff9781c792dd1483a5aaa50e3045a04162ea"): (
        "v1.0.0",
        "c6736e651cf0621e4ffed3397e0c5cc820051f50",
    ),
    ("ORCHESTRATOR.md", "d62c18c5b291f181dfd5ea1137b5cc71b3032b0a58005d29dcb729c6babbd434"): (
        "v0.1.6.20260722-e68f399",
        "e68f399b3536f3f3bc42b9c46b00ecb97bd75193",
    ),
    ("SKILLS.md", "ce9f20c973093c04ada1ee554b17553517406b33730a3969ed93306bf6bbfd9b"): (
        "v0.1.6.20260722-e68f399",
        "e68f399b3536f3f3bc42b9c46b00ecb97bd75193",
    ),
    ("SKILLS.md", "1368bc2f5727cf8c9549a27603bd5331ed65ab431da23a6d7f10e29d7ab23313"): (
        "v0.2.0.20260811-7ee45c0",
        "7ee45c0e6922c3198feacd78d0d37d028bc56240",
    ),
    ("SKILLS.md", "75424a959b3f75703c223a81ca51f5423b6e8bf6f3cc94b97e5697807ac6b39f"): (
        "v1.0.0",
        "c6736e651cf0621e4ffed3397e0c5cc820051f50",
    ),
    ("SOUL.md", "53055a370ce7ee409a61c5f23382d2bd0b3207fc17de734f37ce86e1f8e7591c"): (
        "v0.1.6.20260722-e68f399",
        "e68f399b3536f3f3bc42b9c46b00ecb97bd75193",
    ),
    ("TASK.md", "b4c5b741a347c64990360195273d3f0be7e46275ab674b95fbc7be6b8c934401"): (
        "v0.1.6.20260722-e68f399",
        "e68f399b3536f3f3bc42b9c46b00ecb97bd75193",
    ),
    ("TOOLS.md", "f32c144155cb564e72c766f9acd1e27c4ee7a7dd8311dbe7cf23533047c41476"): (
        "v0.1.6.20260722-e68f399",
        "e68f399b3536f3f3bc42b9c46b00ecb97bd75193",
    ),
    ("USER.md", "31afb46b738ecabb995ce3ec88cba46112c369a49bfaece74f6405acda6ad161"): (
        "v0.1.6.20260722-e68f399",
        "e68f399b3536f3f3bc42b9c46b00ecb97bd75193",
    ),
    ("memory/MEMORY.md", "890d87460fc585a6f44b3db7087cb13e888fcb2582d30cbc1aae2d2b60dbe0a1"): (
        "v0.1.6.20260722-e68f399",
        "e68f399b3536f3f3bc42b9c46b00ecb97bd75193",
    ),
}
