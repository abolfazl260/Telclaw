from services.stage_control import StageControl


def test_stage_skip_is_one_shot_when_consumed():
    control = StageControl()
    assert control.is_skip_requested("crawl") is False
    assert control.request_skip("crawl") is True
    assert control.is_skip_requested("crawl") is True
    assert control.consume_skip("crawl") is True
    assert control.is_skip_requested("crawl") is False
    assert control.consume_skip("crawl") is False


def test_invalid_stage_is_rejected():
    control = StageControl()
    assert control.request_skip("unknown") is False
    assert control.is_skip_requested("unknown") is False
