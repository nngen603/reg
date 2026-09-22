"""Test doubles for the model gateway."""

from regextract.llm.gateway import CallMeta, Gateway

EMPTY_EXTRACTION = {"clause_types": [], "obligations": [], "entities": [], "cross_references": []}


class FailingGateway(Gateway):
    """The offline gateway, except that calls whose tag ends with a given
    suffix fail the way a real provider call does: an error is recorded and an
    empty, schema-shaped payload comes back."""

    def __init__(self, settings, fail_tag_suffix: str):
        super().__init__(settings)
        self.fail_tag_suffix = fail_tag_suffix

    def structured(self, **kwargs):
        if kwargs.get("tag", "").endswith(self.fail_tag_suffix):
            meta = CallMeta(model="simulated", source="live",
                            error="APIConnectionError: simulated provider outage")
            self.stats.add(meta)
            return dict(EMPTY_EXTRACTION), meta
        return super().structured(**kwargs)
