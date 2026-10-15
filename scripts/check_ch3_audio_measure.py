"""独立测量缓存、失败帧数复用和未知请求保护的离线冒烟检查。"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent/'experiments'))
import nlp_ch3_audio_measure as m


def main():
    class Fake:
        calls = 0
        def synthesize(self, *args, timestamps=True):
            assert timestamps is False
            self.calls += 1
            return bytes(48000), {}
    with tempfile.TemporaryDirectory(dir=m.experiment.REPO/'jobs/_validation', prefix='ch3-duration-smoke-') as temp:
        root = Path(temp)
        (root/'tts_requests').mkdir()
        client = Fake()
        args = root, client, 'cosyvoice-v3-flash', 'longanyang', 1.0, 'zh'
        result, pcm = m.measure_chunk('测试一。', *args)
        assert result['samples'] == 24000 and len(pcm) == 48000 and client.calls == 1
        assert m.measure_chunk('测试一。', *args)[0] == result and client.calls == 1
        record = root/'tts_requests'/(result['request']+'.json')
        saved = m.experiment.course.read_json(record)
        m.experiment.dump(record, {**saved, 'state': 'submission_unknown'})
        try:
            m.measure_chunk('测试一。', *args)
        except ValueError:
            pass
        else:
            raise AssertionError('未知请求未拦截')
        assert client.calls == 1
        text = '已有失败诊断。'
        identity = dict(model=args[2], voice=args[3], text=text, rate=1.0, language='zh', timestamps=True)
        key = m.experiment.course.digest(identity)
        m.experiment.dump(root/'tts_requests'/(key+'.json'), {'state': 'submission_unknown', 'characters':len(text)})
        m.experiment.dump(root/'tts_diagnostics'/'d.json', {'state': 'mapping_failed',
            'text_sha256':m.speech.digest(text), 'sample_count':703680, 'usage_record_id':'known'})
        m.experiment.dump(root/'model_usage'/'known.json', {'state':'succeeded'})
        result, pcm = m.measure_chunk(text, *args)
        assert result['samples'] == 703680 and pcm is None and client.calls == 1
        # 不完整失败证据也不能触发重提。
        m.experiment.dump(root/'model_usage'/'known.json', {'state':'started'})
        try:
            m.measure_chunk(text, *args)
        except ValueError:
            pass
        else:
            raise AssertionError('缺少完成证据未拦截')
        assert client.calls == 1
    print('PASS: 时长音频缓存、未知请求拦截、失败诊断帧数复用；未联网')


if __name__ == '__main__':
    main()
