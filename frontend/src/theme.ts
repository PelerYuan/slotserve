import type { GlobalThemeOverrides } from 'naive-ui'

const font =
  "'Inter', ui-sans-serif, -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', 'Noto Sans SC', sans-serif"

export const themeOverrides: GlobalThemeOverrides = {
  common: {
    fontFamily: font,
    fontFamilyMono: "'JetBrains Mono', ui-monospace, 'SF Mono', 'Cascadia Code', Menlo, Consolas, monospace",
    primaryColor: '#4f46e5',
    primaryColorHover: '#5b53f0',
    primaryColorPressed: '#4338ca',
    primaryColorSuppl: '#4f46e5',
    infoColor: '#4f46e5',
    successColor: '#10b981',
    warningColor: '#d97706',
    errorColor: '#e5484d',
    textColorBase: '#17171c',
    textColor1: '#17171c',
    textColor2: '#3a3a45',
    textColor3: '#8e8e9c',
    borderColor: '#e7e7ec',
    dividerColor: '#ececf1',
    borderRadius: '8px',
    borderRadiusSmall: '6px',
    fontSize: '14px',
    fontSizeMedium: '14px',
    heightMedium: '36px',
    boxShadow2: '0 12px 32px -12px rgba(20, 20, 50, 0.18), 0 2px 6px rgba(20,20,40,0.05)',
  },
  Button: {
    fontWeight: '500',
    paddingMedium: '0 16px',
  },
  Card: {
    borderRadius: '12px',
    borderColor: '#e7e7ec',
  },
  Dialog: {
    borderRadius: '14px',
    padding: '24px',
  },
  Input: {
    borderHover: '1px solid #c7c7d1',
  },
  Tag: {
    borderRadius: '6px',
  },
}

export const darkOverrides: GlobalThemeOverrides = {
  common: {
    ...themeOverrides.common,
    primaryColor: '#818cf8',
    primaryColorHover: '#a5b4fc',
    primaryColorPressed: '#6366f1',
    primaryColorSuppl: '#818cf8',
    infoColor: '#818cf8',
    successColor: '#34d399',
    warningColor: '#fbbf24',
    errorColor: '#f87171',
    textColorBase: '#ececf1',
    textColor1: '#ececf1',
    textColor2: '#c4c4cf',
    textColor3: '#8b8b99',
    borderColor: '#2c2c35',
    dividerColor: '#26262e',
    bodyColor: '#111114',
    cardColor: '#18181d',
    modalColor: '#1c1c22',
    popoverColor: '#1f1f26',
    tableColor: '#18181d',
    inputColor: '#141418',
    actionColor: '#1f1f26',
    boxShadow2: '0 12px 32px -12px rgba(0, 0, 0, 0.6), 0 2px 6px rgba(0,0,0,0.3)',
  },
  Button: themeOverrides.Button,
  Card: { borderRadius: '12px', borderColor: '#2c2c35' },
  Dialog: themeOverrides.Dialog,
  Input: { borderHover: '1px solid #3d3d48' },
  Tag: themeOverrides.Tag,
}
