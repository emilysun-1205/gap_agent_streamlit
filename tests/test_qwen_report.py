import json
from unittest.mock import patch, Mock
import pytest
import requests
from gap_agent.qwen_report import analyze_report, build_context, QwenError, SECTIONS

@pytest.fixture
def run(tmp_path):
    (tmp_path/'report.md').write_text('中文报告：摘要层分析',encoding='utf-8')
    (tmp_path/'papers.json').write_text(json.dumps([
        {'paper_id':'P1','include':True,'title':'Real paper','abstract':'A real abstract'},
        {'paper_id':'P2','include':True,'retracted':True,'title':'Retracted'},
        {'paper_id':'P3','include':False,'title':'Excluded'}]),encoding='utf-8')
    return tmp_path

def response(unknown=False):
    data={k:[{'text':'待验证的建议','paper_ids':['fake' if unknown else 'P1']}] for k in SECTIONS}
    return Mock(status_code=200,json=lambda:{'choices':[{'finish_reason':'stop','message':{'content':json.dumps(data)}}],'usage':{'total_tokens':100}})

def test_context_excludes_retracted_and_excluded(run):
    context,refs=build_context(run)
    assert list(refs)==['P1'] and len(context['papers'])==1

def test_success_and_single_request(run):
    with patch('gap_agent.qwen_report.requests.post',return_value=response()) as post:
        value=analyze_report(run,'private-test-key')
    assert value['status']=='ok' and post.call_count==1
    assert post.call_args.kwargs['allow_redirects'] is False
    assert post.call_args.kwargs['json']['enable_thinking'] is False
    assert (run/'qwen_analysis.md').exists()
    assert 'private-test-key' not in (run/'qwen_analysis.json').read_text()

@pytest.mark.parametrize('status',[401,403,429,500])
def test_http_error_does_not_retry_or_overwrite(run,status):
    (run/'qwen_analysis.md').write_text('old result')
    with patch('gap_agent.qwen_report.requests.post',return_value=Mock(status_code=status)) as post:
        with pytest.raises(QwenError):analyze_report(run,'private-test-key')
    assert post.call_count==1 and (run/'qwen_analysis.md').read_text()=='old result'

def test_unknown_citation_rejected(run):
    with patch('gap_agent.qwen_report.requests.post',return_value=response(True)):
        with pytest.raises(QwenError,match='引用'):analyze_report(run,'key')
    assert not (run/'qwen_analysis.json').exists()

def test_timeout_is_redacted(run):
    with patch('gap_agent.qwen_report.requests.post',side_effect=requests.Timeout('secret-key')):
        with pytest.raises(QwenError) as e:analyze_report(run,'secret-key')
    assert 'secret-key' not in str(e.value)

@pytest.mark.parametrize('url',['http://dashscope.aliyuncs.com/compatible-mode/v1','https://evil.example/compatible-mode/v1'])
def test_invalid_host_rejected_before_transmission(run,url):
    with patch('gap_agent.qwen_report.requests.post') as post:
        with pytest.raises(QwenError):analyze_report(run,'key',base_url=url)
    post.assert_not_called()

def test_truncated_output_rejected(run):
    res=response();body=res.json();body['choices'][0]['finish_reason']='length';res.json=lambda:body
    with patch('gap_agent.qwen_report.requests.post',return_value=res):
        with pytest.raises(QwenError,match='完整'):analyze_report(run,'key')
    assert not (run/'qwen_analysis.json').exists()
