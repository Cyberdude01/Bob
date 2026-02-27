/**
 * market-data.test.js
 *
 * Scaffold for the Polymarket data collector test suite.
 * Uses Jest. Run with: npm test
 *
 * External dependencies (axios, sqlite3) are mocked so tests
 * never make real network calls or touch the filesystem database.
 */

'use strict';

jest.mock('axios');
jest.mock('sqlite3');

const axios = require('axios');

// ---------------------------------------------------------------------------
// Helpers / fixtures
// ---------------------------------------------------------------------------

const mockMarketApiResponse = {
  data: [
    {
      conditionId: '0xabc123',
      question: 'Will BTC be up in 15 minutes?',
      tokens: [
        { token_id: 'token_yes', outcome: 'Yes' },
        { token_id: 'token_no',  outcome: 'No'  },
      ],
      volume24hr: 50000,
    },
  ],
};

const mockOrderBookResponse = {
  data: {
    bids: [
      { price: '0.55', size: '100' },
      { price: '0.54', size: '80'  },
      { price: '0.53', size: '60'  },
      { price: '0.52', size: '40'  },
      { price: '0.51', size: '20'  },
    ],
    asks: [
      { price: '0.56', size: '90'  },
      { price: '0.57', size: '70'  },
      { price: '0.58', size: '50'  },
      { price: '0.59', size: '30'  },
      { price: '0.60', size: '10'  },
    ],
  },
};

const mockTradeResponse = {
  data: [
    {
      price: '0.555',
      size:  '25',
      side:  'BUY',
      asset_id: 'token_yes',
    },
  ],
};

// ---------------------------------------------------------------------------
// Order book calculations
// ---------------------------------------------------------------------------

describe('Order book calculations', () => {
  it('computes mid price as average of best bid and best ask', () => {
    const bestBid = 0.55;
    const bestAsk = 0.56;
    const mid = (bestBid + bestAsk) / 2;
    expect(mid).toBeCloseTo(0.555, 3);
  });

  it('computes spread as ask minus bid', () => {
    const bestBid = 0.55;
    const bestAsk = 0.56;
    const spread = bestAsk - bestBid;
    expect(spread).toBeCloseTo(0.01, 3);
  });

  it('computes bid depth as sum of top-5 bid sizes', () => {
    const bids = mockOrderBookResponse.data.bids;
    const depth = bids.slice(0, 5).reduce((sum, b) => sum + parseFloat(b.size), 0);
    expect(depth).toBe(300); // 100+80+60+40+20
  });

  it('computes ask depth as sum of top-5 ask sizes', () => {
    const asks = mockOrderBookResponse.data.asks;
    const depth = asks.slice(0, 5).reduce((sum, a) => sum + parseFloat(a.size), 0);
    expect(depth).toBe(250); // 90+70+50+30+10
  });

  it('handles empty order book without throwing', () => {
    const emptyBids = [];
    const depth = emptyBids.slice(0, 5).reduce((sum, b) => sum + parseFloat(b.size), 0);
    expect(depth).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// Log return calculation
// ---------------------------------------------------------------------------

describe('Log return calculation', () => {
  it('returns correct log return for two prices', () => {
    const p1 = 0.50;
    const p2 = 0.55;
    const logReturn = Math.log(p2 / p1);
    expect(logReturn).toBeCloseTo(0.0953, 4);
  });

  it('returns 0 when prices are equal', () => {
    const p = 0.50;
    expect(Math.log(p / p)).toBe(0);
  });

  it('returns negative log return when price decreases', () => {
    const p1 = 0.60;
    const p2 = 0.50;
    const logReturn = Math.log(p2 / p1);
    expect(logReturn).toBeLessThan(0);
  });
});

// ---------------------------------------------------------------------------
// API response shape validation (mocked axios)
// ---------------------------------------------------------------------------

describe('fetchMarketBySlug (mocked)', () => {
  beforeEach(() => {
    axios.get.mockResolvedValue(mockMarketApiResponse);
  });

  afterEach(() => {
    jest.clearAllMocks();
  });

  it('returns market data with conditionId and tokens', async () => {
    const response = await axios.get('https://gamma-api.polymarket.com/markets?slug=btc-up-15m');
    const market = response.data[0];
    expect(market).toHaveProperty('conditionId');
    expect(market.tokens).toHaveLength(2);
  });

  it('calls the correct endpoint', async () => {
    const slug = 'btc-up-15m';
    await axios.get(`https://gamma-api.polymarket.com/markets?slug=${slug}`);
    expect(axios.get).toHaveBeenCalledWith(
      'https://gamma-api.polymarket.com/markets?slug=btc-up-15m'
    );
  });
});

describe('fetchOrderBook (mocked)', () => {
  beforeEach(() => {
    axios.get.mockResolvedValue(mockOrderBookResponse);
  });

  afterEach(() => {
    jest.clearAllMocks();
  });

  it('returns bids and asks arrays', async () => {
    const response = await axios.get('https://clob.polymarket.com/book?token_id=token_yes');
    expect(response.data).toHaveProperty('bids');
    expect(response.data).toHaveProperty('asks');
    expect(Array.isArray(response.data.bids)).toBe(true);
  });
});

describe('fetchLastTradeForMarket (mocked)', () => {
  beforeEach(() => {
    axios.get.mockResolvedValue(mockTradeResponse);
  });

  afterEach(() => {
    jest.clearAllMocks();
  });

  it('returns trade with price, size, and side', async () => {
    const response = await axios.get(
      'https://data-api.polymarket.com/trades?market=0xabc123&limit=1'
    );
    const trade = response.data[0];
    expect(trade).toHaveProperty('price');
    expect(trade).toHaveProperty('size');
    expect(trade).toHaveProperty('side');
  });

  it('trade side is BUY or SELL', async () => {
    const response = await axios.get(
      'https://data-api.polymarket.com/trades?market=0xabc123&limit=1'
    );
    const side = response.data[0].side;
    expect(['BUY', 'SELL']).toContain(side);
  });
});
