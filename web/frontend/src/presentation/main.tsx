import React from 'react';
import ReactDOM from 'react-dom/client';
import { ConfigProvider } from 'tdesign-react';
import zhConfig from 'tdesign-react/es/locale/zh_CN';
import 'tdesign-react/es/style/index.css';
import '../styles.css';
import './presentation.css';
import { AccessGate } from '../AccessGate';
import Presentation from './Presentation';

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode><ConfigProvider globalConfig={{ ...zhConfig, classPrefix: 't' }}>
    <AccessGate><Presentation /></AccessGate>
  </ConfigProvider></React.StrictMode>,
);
