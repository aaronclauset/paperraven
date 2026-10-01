"""PaperRaven — keywords to a verified corpus of real papers.

The package is domain-agnostic: it knows nothing about the subject matter of any corpus it
builds.
"""

#: Recorded on every audit event, so a corpus says which code produced it.
#: `Politeness.user_agent_version` carries the same value for a different purpose — identifying
#: the tool to a metadata registry — and is set in config. The two must agree, and are kept
#: apart rather than coupled.
__version__ = "0.1.0"
