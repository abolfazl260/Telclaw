from rapidfuzz.fuzz import ratio

class DuplicateDetector:
    def __init__(self):
        self.storage = {}

    def check_duplicate(self, sender_id, category, text):
        key = (str(sender_id), category)

        if key not in self.storage:
            self.storage[key] = []

        for old_text in self.storage[key]:
            similarity = ratio(text, old_text) / 100
            if similarity >= 0.8:
                return True, similarity

        self.storage[key].append(text)
        return False, 0
