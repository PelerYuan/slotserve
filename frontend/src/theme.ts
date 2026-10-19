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
