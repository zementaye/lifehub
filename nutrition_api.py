"""Thin wrapper around the USDA FoodData Central API (free, api.data.gov)."""

import requests

import config

BASE_URL = "https://api.nal.usda.gov/fdc/v1"

# Map the USDA nutrient names we care about to our internal keys
_NUTRIENT_MAP = {
    "Energy": "calories",
    "Protein": "protein_g",
    "Carbohydrate, by difference": "carbs_g",
    "Total lipid (fat)": "fat_g",
    "Fiber, total dietary": "fiber_g",
}


def _extract_nutrients(food: dict) -> dict:
    out = {"calories": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0, "fiber_g": 0.0}
    for n in food.get("foodNutrients", []):
        name = n.get("nutrientName")
        key = _NUTRIENT_MAP.get(name)
        if key:
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
    """Search USDA foods. All nutrient values are per 100g (that's how FDC
    stores them for every data type, branded included).

    Generic whole foods (Foundation / SR Legacy — USDA's standardized,
    unbranded entries) are fetched and ranked first, since a plain search
    like "egg" or "banana" tends to get buried under branded products
    (snack bars, prepared dishes, etc.) that happen to share the word and
    can have wildly different calorie counts. Branded results only fill in
    the remaining slots if there aren't enough generic matches.
    """
    if not query.strip():
        return []

    generic_foods = _fetch(query, "Foundation,SR Legacy", page_size)
    seen_ids = {f.get("fdcId") for f in generic_foods}

    branded_foods = []
    if len(generic_foods) < page_size:
        branded_foods = [
            f for f in _fetch(query, "Branded", page_size - len(generic_foods))
            if f.get("fdcId") not in seen_ids
        ]

    results = []
    for food in generic_foods + branded_foods:
        nutrients = _extract_nutrients(food)
        results.append({
            "fdc_id": food.get("fdcId"),
            "name": food.get("description", "Unknown"),
            "brand": food.get("brandOwner", ""),
            "generic": food.get("dataType") in ("Foundation", "SR Legacy"),
            **nutrients,
        })
    return results
