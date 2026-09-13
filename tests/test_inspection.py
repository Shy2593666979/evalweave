from openpyxl import Workbook

from evalweave.agents.inspection import inspect_source, load_source_rows


def test_xlsx_parser_corrects_model_header_row_for_grouped_headers(tmp_path) -> None:
    stored_path = tmp_path / "grouped-header-workbook"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["班级综合考评", None, None])
    sheet.append([None, None, "学习成绩"])
    sheet.append(["学号", "姓名", "绩点"])
    sheet.append(["2128824002", "徐闻", 4.03])
    sheet.append([None, None, None])
    sheet.append(["2128824005", "马苑森", 3.61])
    sheet.append(["   ", None, None])
    workbook.save(stored_path)
    workbook.close()

    rows = load_source_rows(
        stored_path,
        "scores.xlsx",
        100,
        header_row=2,
        data_start_row=3,
        field_names=["student_id", "student_name", "gpa"],
    )

    assert rows == [
        {"student_id": "2128824002", "student_name": "徐闻", "gpa": 4.03},
        {"student_id": "2128824005", "student_name": "马苑森", "gpa": 3.61},
    ]


def test_reads_xlsx_from_extensionless_storage_path(tmp_path) -> None:
    stored_path = tmp_path / "0123456789abcdef"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "评测集"
    sheet.append(["message", "expected"])
    sheet.append(["你好", "您好，有什么可以帮助您？"])
    workbook.save(stored_path)
    workbook.close()

    summary = inspect_source(stored_path, "cases.xlsx", 5)
    rows = load_source_rows(stored_path, "cases.xlsx", 100)

    assert summary["format"] == "xlsx"
    assert summary["fields"] == ["message", "expected"]
    assert summary["sheet"] == "评测集"
    assert rows == [{"message": "你好", "expected": "您好，有什么可以帮助您？"}]
