"""Thin wrapper around the USDA FoodData Central API (free, api.data.gov)."""

import requests

import common_foods
import config

BASE_URL = "https://api.nal.usda.gov/fdc/v1"

# Map the USDA nutrient names we care about to our internal keys, along
# with the unit each one must be reported in.
_NUTRIENT_MAP = {
    "Energy": ("calories", "KCAL"),
    "Protein": ("protein_g", "G"),
    "Carbohydrate, by difference": ("carbs_g", "G"),
    "Total lipid (fat)": ("fat_g", "G"),
    "Fiber, total dietary": ("fiber_g", "G"),
}


def _extract_nutrients(food: dict) -> dict:
    out = {"calories": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0, "fiber_g": 0.0}
    for n in food.get("foodNutrients", []):
        mapping = _NUTRIENT_MAP.get(n.get("nutrientName"))
        if not mapping:
            continue
        key, expected_unit = mapping
        # USDA records commonly list "Energy" TWICE — once in kcal, once in
        # kJ (~4.18x larger). Matching by name alone let whichever one came
        # last in the array silently win, which is what caused wildly
        # inflated calorie counts (e.g. an egg showing 617 instead of 143).
        # Only accept the entry that's actually in the expected unit.
        unit = (n.get("unitName") or "").upper()
        if unit and unit != expected_unit:
            continue
        out[key] = n.get("value", 0.0) or 0.0
    return out


def _fetch(query: str, data_types: str, page_size: int) -> list[dict]:
    try:
        resp = requests.get(
            f"{BASE_URL}/foods/search",
            params={
                "api_key": config.USDA_API_KEY,
                "query": query,
                "pageSize": page_size,
                "dataType": data_types,
            },
            timeout=10,
        )
        resp.raise_for_status()
    except requests.RequestException:
        return []
    return resp.json().get("foods", [])


def search_foods(query: str, page_size: int = 15) -> list[dict]:
    """Search for foods. The local common-foods database is checked first
    and, if it has ANY match, that's all that's returned — no USDA results
    get mixed in. USDA's database often has 5+ wildly different entries for
    something as simple as "chicken breast" (raw, cooked, breaded tenders,
    lunchmeat, roll...), and showing all of them next to a curated single
    correct answer just re-introduces the exact confusion the local
    database exists to avoid. USDA is only used as a fallback for foods
    that aren't in the curated local list at all.

    All nutrient values are per 100g.
    """
    if not query.strip():
        return []

    local_results = common_foods.search_common_foods(query)
    if local_results:
        return local_results

    generic_foods = _fetch(query, "Foundation,SR Legacy", page_size)
    seen_ids = {f.get("fdcId") for f in generic_foods}

    branded_foods = []
    if len(generic_foods) < page_size:
        branded_foods = [
            f for f in _fetch(query, "Branded", page_size - len(generic_foods))
            if f.get("fdcId") not in seen_ids
        ]

    usda_results = []
    for food in generic_foods + branded_foods:
        nutrients = _extract_nutrients(food)
        usda_results.append({
            "fdc_id": food.get("fdcId"),
            "name": food.get("description", "Unknown"),
            "brand": food.get("brandOwner", ""),
            "generic": food.get("dataType") in ("Foundation", "SR Legacy"),
            **nutrients,
        })
    return usda_results
