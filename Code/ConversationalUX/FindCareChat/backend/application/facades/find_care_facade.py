# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# DEPRECATED — FindCareFacade collapsed into FindCareService (Build 371).
# This file exists only for backward compatibility.
# Import FindCareService from domain.find_care.provider_search_service instead.

from domain.find_care.provider_search_service import FindCareService as FindCareFacade

__all__ = ["FindCareFacade"]
