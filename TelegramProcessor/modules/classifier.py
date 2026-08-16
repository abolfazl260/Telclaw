import json

class Classifier:
    def __init__(self, config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            self.categories = json.load(f)

    def classify(self, text):
        scores = {}
        for category, config in self.categories.items():
            score = 0
            for keyword in config["keywords"]:
                if keyword.lower() in text:
                    score += config.get("weight", 1)
            scores[category] = score

        best = max(scores, key=lambda x: scores[x]) if scores else "Other"
        return best if scores.get(best, 0) > 0 else "Other"
