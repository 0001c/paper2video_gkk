"""本机审核 CLI：读回完整 JSON 后编辑 pages，再全量保存。仅依赖标准库。"""
import argparse
import json
import sys
import re
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from urllib.parse import urlparse


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('job_id')
    parser.add_argument('--base', default='http://127.0.0.1:8848')
    parser.add_argument('--save', type=Path, help='含 version、pages 的 JSON 文件；PUT 全量替换')
    parser.add_argument('--render', action='store_true', help='明确确认讲稿后开始配音合片')
    parser.add_argument('--regenerate-page', type=int, help='调用模型重写指定页（从 1 开始，可能产生费用）')
    parser.add_argument('--dry-run', action='store_true', help='只校验本地文件，不发请求')
    args = parser.parse_args()
    try:
        if not re.fullmatch(r'[A-Za-z0-9_-]+', args.job_id) or urlparse(args.base).hostname not in ('127.0.0.1', 'localhost', '::1'):
            raise ValueError('只支持本机地址和合法任务 ID')
        body = json.loads(args.save.read_text(encoding='utf-8')) if args.save else None
        if body is not None:
            body = {'version': body['version'], 'pages': body['pages']}
            if not isinstance(body['version'], str) or len(body['version']) != 64 or not isinstance(body['pages'], list) or not body['pages']:
                raise ValueError('version/pages 无效')
            sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Paper2Video-main' / 'src'))
            from script_validation import validate_script_output
            if not validate_script_output('\n###\n'.join(body['pages']), len(body['pages']))[0]:
                raise ValueError('pages 格式不正确')
        if args.dry_run:
            if body is None:
                raise ValueError('--dry-run 需提供 --save JSON')
            print(json.dumps({'ok': True, 'dry_run': True}))
            return 0
        method = 'PUT' if body else 'GET'
        route = '/api/jobs/' + args.job_id + '/review'
        if args.render or args.regenerate_page:
            if body is None:
                raise ValueError('确认操作需提供 --save JSON')
            body.update({'action': 'regenerate' if args.regenerate_page else 'render',
                         'page_ids': [args.regenerate_page - 1] if args.regenerate_page else []})
            route += '/run'
            method = 'POST'
        request = Request(args.base.rstrip('/') + route, data=json.dumps(body).encode('utf-8') if body else None,
                          headers={'Content-Type': 'application/json'}, method=method)
        with urlopen(request, timeout=120) as response:
            print(response.read().decode('utf-8'))
        return 0
    except HTTPError as exc:
        print(exc.read().decode('utf-8'), file=sys.stderr)
        return 1
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
