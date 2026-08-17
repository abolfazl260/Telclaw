import os
import pandas as pd
from tqdm import tqdm
from modules.cleaner import clean_dataframe
from modules.classifier import Classifier
from modules.duplicate_detector import DuplicateDetector
from modules.exporter import Exporter
from modules.logger import setup_logger

# Base directory of TelegramProcessor project
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

INPUT_FOLDER = os.path.join(
    BASE_DIR,
    "input"
)

CONFIG_FILE = os.path.join(
    BASE_DIR,
    "config",
    "categories.json"
)

OUTPUT_FOLDER = os.path.join(
    BASE_DIR,
    "normalized_messages"
)

CHUNK_SIZE = 5000
logger = setup_logger()

def process():
    classifier = Classifier(CONFIG_FILE)
    duplicate_detector = DuplicateDetector()
    exporter = Exporter(OUTPUT_FOLDER)

    duplicate_records = []

    processed_messages = 0
    saved_messages = 0
    duplicate_messages = 0

    try:
        # The input directory is runtime data and may not exist on a fresh checkout.
        # Create it automatically instead of failing with FileNotFoundError.
        os.makedirs(INPUT_FOLDER, exist_ok=True)

        # پیدا کردن تمام فایل‌های CSV که در نام آنها Message وجود دارد
        input_files = sorted([
            os.path.join(INPUT_FOLDER, file)
            for file in os.listdir(INPUT_FOLDER)
            if file.lower().endswith(".csv")
            and "message" in file.lower()
        ])

        if not input_files:
            print(f"No input CSV files containing 'Message' were found in: {INPUT_FOLDER}")
            print("Place the Telegram export CSV files in this folder and run the processor again.")
            return

        print(f"Found {len(input_files)} input file(s).")

        for input_file in input_files:

            print("\n===================================")
            print(f"Processing : {os.path.basename(input_file)}")
            print("===================================")

            with open(input_file, encoding="utf-8") as file_handle:
                total_rows = sum(1 for _ in file_handle) - 1

            if total_rows < 0:
                total_rows = 0

            progress = tqdm(
                total=total_rows,
                desc=os.path.basename(input_file)
            )

            for chunk in pd.read_csv(
                input_file,
                chunksize=CHUNK_SIZE,
                encoding="utf-8"
            ):

                chunk = clean_dataframe(chunk)

                for _, row in chunk.iterrows():

                    processed_messages += 1

                    category = classifier.classify(
                        row["normalized_text"]
                    )

                    duplicate, similarity = duplicate_detector.check_duplicate(
                        row["sender_id"],
                        category,
                        row["normalized_text"]
                    )

                    if duplicate:
                        duplicate_records.append({
                            "message_id": row["message_id"],
                            "sender_id": row["sender_id"],
                            "category": category,
                            "similarity_score": similarity
                        })

                        duplicate_messages += 1
                        continue

                    output = {
                        "Number": saved_messages + 1,
                        "message_id": row["message_id"],
                        "unique_message_key": row["unique_message_key"],
                        "date": row["date"],
                        "channel_id": row["channel_id"],
                        "channel_username": row["channel_username"],
                        "channel_name": row["channel_name"],
                        "sender_id": row["sender_id"],
                        "sender_username": row["sender_username"],
                        "sender_type": row["sender_type"],
                        "text": row["text"],
                        "media_type": row["media_type"],
                        "file_unique_id": row["file_unique_id"],
                        "categories": category
                    }

                    exporter.save(
                        category,
                        pd.DataFrame([output])
                    )

                    saved_messages += 1

                progress.update(len(chunk))

            progress.close()

            # تغییر نام فایل بعد از پایان پردازش
            base_name, extension = os.path.splitext(input_file)
            processed_file = f"{base_name}_Processed{extension}"

            try:
                if os.path.exists(processed_file):
                    os.remove(processed_file)

                os.rename(
                    input_file,
                    processed_file
                )

                print(
                    f"Renamed to: {os.path.basename(processed_file)}"
                )

            except Exception as rename_error:
                print(f"Rename failed: {rename_error}")

        exporter.save_duplicates(duplicate_records)

        print("\n==============================")
        print("Processing Completed")
        print("==============================")
        print(f"Total processed messages : {processed_messages}")
        print(f"Saved messages           : {saved_messages}")
        print(f"Duplicate messages       : {duplicate_messages}")
        print("==============================")

        logger.info(
            f"Processing completed. Total={processed_messages}, Saved={saved_messages}, Duplicate={duplicate_messages}"
        )

    except Exception as e:
        print("\nERROR:")
        print(e)
        logger.exception(f"Processing failed: {e}")
        raise


if __name__ == "__main__":
    process()
