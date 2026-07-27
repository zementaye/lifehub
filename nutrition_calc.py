"""Recommended daily nutrition intake.

Uses the Mifflin-St Jeor BMR formula (height, weight, age, sex), scaled by
an activity multiplier derived from ACTUAL logged workout sessions in the
past 7 days — using real data already in the Health page's session log,
rather than asking the person to self-report how active they are.
"""


def age_from_birth_date(birth_date_str: str, today) -> int:
    y, m, d = (int(p) for p in birth_date_str.split("-"))
    return today.year - y - ((today.month, today.day) < (m, d))


def activity_multiplier_from_sessions(session_count: int) -> tuple[float, str]:
    """Standard TDEE activity multipliers, chosen from how many workout
    sessions were actually logged in the last 7 days."""
    if session_count <= 0:
        return 1.2, "Sedentary — no logged workouts this week"
    if session_count <= 2:
        return 1.375, f"Lightly active — {session_count} workout(s) this week"
    if session_count <= 4:
        return 1.55, f"Moderately active — {session_count} workouts this week"
    if session_count <= 6:
        return 1.725, f"Very active — {session_count} workouts this week"
    return 1.9, f"Extra active — {session_count} workouts this week"


def compute_recommendation(height_cm, weight_kg, birth_date_str, sex, today, session_count):
    """Returns a dict of recommended daily targets, or None if required
    profile fields (height, weight, birth date, sex) aren't all set yet."""
    if not (height_cm and weight_kg and birth_date_str and sex):
        return None

    age = age_from_birth_date(birth_date_str, today)
    if sex == "male":
        bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age + 5
    else:
        bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age - 161

    multiplier, activity_label = activity_multiplier_from_sessions(session_count)
    tdee = bmr * multiplier

    protein_g = weight_kg * 1.8  # solid general-fitness target per kg bodyweight
    fat_g = (tdee * 0.25) / 9    # ~25% of calories from fat
    carbs_g = max(0.0, tdee - (protein_g * 4) - (fat_g * 9)) / 4  # remainder
    fiber_g = (tdee / 1000) * 14  # standard ~14g per 1000 kcal guideline

    return {
        "age": age,
        "bmr": round(bmr),
        "tdee": round(tdee),
        "calories": round(tdee),
        "protein_g": round(protein_g),
        "fat_g": round(fat_g),
        "carbs_g": round(carbs_g),
        "fiber_g": round(fiber_g),
        "activity_label": activity_label,
    }
