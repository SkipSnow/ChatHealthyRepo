# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Specialty Ranker — uses GPT-4.1-mini to sort specialty options
# by relevance to the user's search query. The most clinically
# relevant specialties appear first in the filter panel.
#
# One call per search. Fast (mini model). The ranking is contextual —
# "find me a kids doc" ranks Pediatrics first, "chest pain" ranks
# Cardiology first. Same list, different order.

import json
import logging
import os

from openai import OpenAI

_log = logging.getLogger("findcare.specialty_ranker")
