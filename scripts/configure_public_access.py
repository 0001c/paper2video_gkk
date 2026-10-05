import argparse
import csv
import getpass
import ipaddress
import json
import secrets
import sys
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from web.public_access import normalize_origin, password_digest


def main():
    parser = argparse.ArgumentParser(description='配置单管理员公网HTTPS访问；不会自动修改穿透或启动服务')
    parser.add_argument('--origin', required=True, help='确切公网HTTPS来源，含端口')
    parser.add_argument('--directory', default=str(ROOT / '.local/public-access'))
    parser.add_argument('--username', default='admin')
    parser.add_argument('--generate-password', action='store_true', help='生成随机密码并写入受保护的本机credentials.txt')
    parser.add_argument('--local-ip', action='append', default=['127.0.0.1'], help='需要包含在自签名证书中的局域网IP')
    args = parser.parse_args()
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    origin = normalize_origin(args.origin)
    password = secrets.token_urlsafe(24) if args.generate_password else getpass.getpass('设置管理员密码（至少16位，不会回显）：')
    if len(password) < 16 or len(password) > 256:
        parser.error('管理员密码必须为16至256位')
    salt = secrets.token_hex(16)
    config = {'public_origins': [origin], 'username': args.username, 'password_salt': salt,
              'password_digest': password_digest(password, salt), 'tls_cert': 'server-cert.pem', 'tls_key': 'server-key.pem'}
    from web.public_access import PublicAccess
    PublicAccess([origin], args.username, salt, config['password_digest'])
    directory = Path(args.directory).resolve()
    if directory.exists() and any(directory.iterdir()):
        parser.error('目标目录非空；为避免覆盖凭据和证书，请明确使用新的空目录')
    directory.mkdir(parents=True, exist_ok=True)
    if sys.platform == 'win32':
        identity = subprocess.run(['whoami', '/user', '/fo', 'csv', '/nh'], check=True, capture_output=True, text=True)
        sid = next(csv.reader(identity.stdout.splitlines()))[1]
        subprocess.run(['icacls', str(directory), '/inheritance:r', '/grant:r', f'*{sid}:(OI)(CI)F',
                        '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F'], check=True, capture_output=True)
    else:
        directory.chmod(0o700)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    hostname = urlsplit(origin).hostname
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    names = [x509.DNSName('localhost')]
    for address in set(args.local_ip + [hostname]):
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(address)))
        except ValueError:
            names.append(x509.DNSName(address))
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                   .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
                   .not_valid_after(now + timedelta(days=90)).add_extension(x509.SubjectAlternativeName(names), critical=False)
                   .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                   .sign(key, hashes.SHA256()))
    (directory / 'server-key.pem').write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    (directory / 'server-cert.pem').write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    (directory / 'server-cert.cer').write_bytes(certificate.public_bytes(serialization.Encoding.DER))
    (directory / 'config.json').write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
    if args.generate_password:
        (directory / 'credentials.txt').write_text(f'公网地址：{origin}/\n账号：{args.username}\n密码：{password}\n请妥善保存，切勿提交或分享。\n', encoding='utf-8')
    if sys.platform != 'win32':
        for filename in ('server-key.pem', 'config.json', 'credentials.txt'):
            path = directory / filename
            if path.exists():
                path.chmod(0o600)
    print(f'配置已保存：{directory / "config.json"}')
    print(f'证书SHA256指纹：{certificate.fingerprint(hashes.SHA256()).hex()}')
    print('自签名证书必须经核对指纹后在访问设备上手动信任；正式部署应更换为受信任证书。')
    if args.generate_password:
        print(f'登录凭据仅保存于本机：{directory / "credentials.txt"}')


if __name__ == '__main__':
    main()
