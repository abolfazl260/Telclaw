import os
import pandas as pd

class Exporter:

    def __init__(self, output):
        self.output = output

        os.makedirs(
            output,
            exist_ok=True
        )


    def save(self, category, df):

        # تبدیل نام دسته به نام فایل
        filename = f"{category.lower()}.csv"

        path = os.path.join(
            self.output,
            filename
        )

        df.to_csv(
            path,
            mode="a",
            index=False,
            header=not os.path.exists(path),
            encoding="utf-8-sig"
        )


    def save_duplicates(self, records):

        if records:

            pd.DataFrame(records).to_csv(
                os.path.join(
                    self.output,
                    "duplicate_log.csv"
                ),
                index=False,
                encoding="utf-8-sig"
            )
