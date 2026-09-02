# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# EmbeddingClient — the specialty-catalogue embedding call.
#
# Names no model. There is one embedding model across the whole
# application, declared once for the firm and read by the facade from the
# binding this target carries.

from chathealthy_lib.llm import embed


class EmbeddingClient:
    """The embedding used to recall specialty candidates."""

    def get_specialty_vector(self, text: str) -> list:
        """Embed the clinical search term for specialty matching.

        Same model as the provider embeddings, which is what makes the
        cross-collection recall meaningful -- and it is the same because
        there is one declaration, not because two sites agree.

        Raises on failure (no fallback per EPIC-006-F-003-S-001).
        SpecialtyFilter's find_specialties() is the single catch point and
        surfaces the actual upstream cause to the frontend.
        """
        return embed(
            text,
            call_site="EmbeddingClient.get_specialty_vector",
            provider="openai", server="find_care", component="EmbeddingClient")
