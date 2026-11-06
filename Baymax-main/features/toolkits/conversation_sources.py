"""Cached recipe discovery."""

from .public_sources import PublicSources, bounded_text
from .source_helpers import text, rows


class ConversationSources(PublicSources):
    def meals(self, query):
        data, info = self._json("https://www.themealdb.com/api/json/v1/1/search.php",
                               {"s": bounded_text(query)}, 3600)
        if not isinstance(data, dict):
            raise ValueError("Invalid recipe response")
        meals = []
        for m in rows(data.get("meals") or [])[:3]:
            ingredients = [text((m.get(f"strMeasure{i}") or "") + " " + m[f"strIngredient{i}"], 200)
                           for i in range(1, 21) if m.get(f"strIngredient{i}")]
            meals.append({"name": text(m.get("strMeal")), "ingredients": ingredients,
                          "instructions": text(m.get("strInstructions") or "", 4000),
                          "url": text(m.get("strSource") or "")})
        return {**info, "source": "TheMealDB", "meals": meals,
                "notice": "Recipes do not guarantee allergen or dietary suitability. Test key is for development/education; public app-store release requires supporter access."}
