const base = require('./playwright.config.cjs');
module.exports = {
  ...base,
  testMatch: 'smoke.spec.cjs',
  webServer: undefined,
  use: { ...base.use, baseURL: process.env.UNDERWRITER_SMOKE_URL || 'https://underwriter.ianlan.dev' },
};
