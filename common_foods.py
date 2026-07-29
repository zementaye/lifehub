"""A curated set of common foods with standard per-100g nutrition values,
so basic searches (chicken breast, rice, eggs...) always return something
useful instantly, without depending on a third-party API being up, fast,
or under its rate limit. Values are standard reference figures (raw/cooked
as commonly tracked) consistent with USDA and other major nutrition
databases for whole, unbranded foods.

Each entry: (name, calories, protein_g, carbs_g, fat_g, fiber_g) — all per 100g.
"""

COMMON_FOODS = [
    # Proteins
    ("Chicken breast, cooked, skinless", 165, 31.0, 0.0, 3.6, 0.0),
    ("Chicken thigh, cooked, skinless", 209, 26.0, 0.0, 10.9, 0.0),
    ("Beef, ground 85% lean, cooked", 250, 26.0, 0.0, 15.0, 0.0),
    ("Egg, whole, raw", 143, 12.6, 0.7, 9.5, 0.0),
    ("Salmon, cooked", 208, 20.0, 0.0, 13.0, 0.0),
    ("Tuna, canned in water", 116, 26.0, 0.0, 1.0, 0.0),
    ("Shrimp, cooked", 99, 24.0, 0.2, 0.3, 0.0),
    ("Turkey breast, cooked", 135, 30.0, 0.0, 1.0, 0.0),
    ("Tofu, firm", 144, 15.5, 3.9, 8.7, 2.3),
    ("Greek yogurt, plain, nonfat", 59, 10.0, 3.6, 0.4, 0.0),

    # Grains & carbs
    ("White rice, cooked", 130, 2.7, 28.0, 0.3, 0.4),
    ("Brown rice, cooked", 123, 2.7, 26.0, 1.0, 1.6),
    ("Injera (teff flatbread)", 166, 5.5, 34.0, 1.0, 3.0),
    ("Teff grain, raw", 367, 13.3, 73.0, 2.4, 8.0),
    ("Bread, white", 265, 9.0, 49.0, 3.2, 2.7),
    ("Bread, whole wheat", 247, 13.0, 41.0, 3.4, 6.8),
    ("Pasta, cooked", 131, 5.0, 25.0, 1.1, 1.8),
    ("Oats, dry", 389, 16.9, 66.0, 6.9, 10.6),
    ("Potato, boiled", 87, 1.9, 20.0, 0.1, 1.8),
    ("Sweet potato, boiled", 90, 2.0, 21.0, 0.1, 3.0),
    ("Quinoa, cooked", 120, 4.4, 21.0, 1.9, 2.8),

    # Legumes
    ("Lentils, cooked", 116, 9.0, 20.0, 0.4, 7.9),
    ("Chickpeas, cooked", 164, 8.9, 27.0, 2.6, 7.6),
    ("Black beans, cooked", 132, 8.9, 24.0, 0.5, 8.7),
    ("Shiro (chickpea flour stew)", 120, 5.5, 15.0, 4.5, 3.0),

    # Dairy & fats
    ("Milk, whole", 61, 3.2, 4.8, 3.3, 0.0),
    ("Milk, skim", 34, 3.4, 5.0, 0.1, 0.0),
    ("Cheese, cheddar", 403, 25.0, 1.3, 33.0, 0.0),
    ("Butter", 717, 0.9, 0.1, 81.0, 0.0),
    ("Olive oil", 884, 0.0, 0.0, 100.0, 0.0),
    ("Peanut butter", 588, 25.0, 20.0, 50.0, 6.0),
    ("Almonds", 579, 21.0, 22.0, 50.0, 12.5),

    # Fruits
    ("Banana", 89, 1.1, 23.0, 0.3, 2.6),
    ("Apple", 52, 0.3, 14.0, 0.2, 2.4),
    ("Orange", 47, 0.9, 12.0, 0.1, 2.4),
    ("Mango", 60, 0.8, 15.0, 0.4, 1.6),
    ("Avocado", 160, 2.0, 8.5, 15.0, 6.7),

    # Vegetables
    ("Broccoli", 34, 2.8, 7.0, 0.4, 2.6),
    ("Spinach", 23, 2.9, 3.6, 0.4, 2.2),
    ("Carrot", 41, 0.9, 10.0, 0.2, 2.8),
    ("Tomato", 18, 0.9, 3.9, 0.2, 1.2),
    ("Onion", 40, 1.1, 9.3, 0.1, 1.7),
]


def search_common_foods(query: str) -> list[dict]:
    q = query.strip().lower()
    if not q:
        return []
    results = []
    for name, cal, protein, carbs, fat, fiber in COMMON_FOODS:
        if q in name.lower():
            results.append({
                "fdc_id": None,
                "name": name,
                "brand": "",
                "generic": True,
                "calories": cal,
                "protein_g": protein,
                "carbs_g": carbs,
                "fat_g": fat,
                "fiber_g": fiber,
            })
    return results
