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


def search_foods(query: str, page_size: int = 15) -> list[dict]:
    """Search USDA foods. Returns per-100g nutrition (or per-serving where that's
    all USDA provides) for each match, ready to scale by servings in the UI."""
    if not query.strip():
        return []
    try:
        resp = requests.get(
            f"{BASE_URL}/foods/search",
            params={
                "api_key": config.USDA_API_KEY,
                "query": query,
                "pageSize": page_size,
                "dataType": "Foundation,SR Legacy,Branded",
            },
            timeout=10,
        )
        resp.raise_for_status()
    except requests.RequestException:
        return []

    data = resp.json()
    results = []
    for food in data.get("foods", []):
        nutrients = _extract_nutrients(food)
        results.append({
            "fdc_id": food.get("fdcId"),
            "name": food.get("description", "Unknown"),
            "brand": food.get("brandOwner", ""),
            **nutrients,
        })
    return results
