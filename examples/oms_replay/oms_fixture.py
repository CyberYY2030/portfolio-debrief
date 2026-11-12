"""Build a wholly fictional OMS export in the real three-file format. Every name, code and number here is invented."""
import argparse
import zipfile
from decimal import Decimal as D
from pathlib import Path
from xml.sax.saxutils import escape

HEADERS = {
    "各产品持仓": [
        "日期",
        "产品",
        "单元编号",
        "单元",
        "交易市场",
        "证券代码",
        "证券名称",
        "持仓市值（本币）",
        "市值/总资产(%)",
        "最新价",
        "持仓数量",
        "总股本",
        "持仓/总股本(%)",
        "当日涨跌幅（%）",
        "期初数量",
        "板块类型",
        "可用",
        "当前成本（全价人民币）",
        "成本价（全价人民币）",
        "当日变动数量",
        "当前成本（全价本币）",
        "成本价（全价本币）",
        "成本价(净价)",
        "实现盈亏",
        "浮动盈亏",
        "盯市盈亏",
        "累计盈亏",
        "浮盈率（%）",
        "损益率（%）",
        "多空标志",
        "币种",
        "证券类别",
        "资产类别",
        "当日实现盈亏",
        "停牌标志",
        "流通股本",
        "持仓/流通股本(%)",
        "成本/总资产(%)",
        "市值/净资产(%)",
        "市值/单元净资产(%)",
        "当日买入数量",
        "当日买入均价",
        "当日卖出数量",
        "当日卖出均价",
        "股东代码",
        "资金账号",
        "投资类型",
        "持仓市值(人民币)",
        "持仓市值(净价)",
        "持仓市值(净价)/净资产(%)",
        "总体盈亏率(%)",
        "当日买金额",
        "当日卖金额",
        "保本价",
        "成本/单元净值(%)",
        "托管机构",
        "保证金占用",
        "产品编号",
        "持仓证券类型",
        "当前成本(净价)",
        "持仓市值(全价)",
        "债券利息",
        "持仓市值(净价)/单元净值(%)",
        "持仓市值(全价)/单元净值(%)",
        "当前成本(净价)/单元净值(%)",
        "当前成本(全价人民币)/单元净值(%)",
        "是否尚未盈利",
        "是否具有协议控制架构",
        "是否存在投票权差异",
        "产品序号",
        "浮动盈亏（人民币）",
        "柜台成本价",
        "报盘方式",
        "结算方式"
    ],
    "成交查询": [
        "成交编号",
        "证券代码[委托方向]",
        "证券代码",
        "单元",
        "产品序号",
        "产品",
        "发生日期",
        "证券名称",
        "板块类型",
        "交易员编号",
        "交易员姓名",
        "委托方向",
        "头寸性质",
        "备兑开仓标志",
        "成交数量",
        "成交均价",
        "全价成交均价",
        "成交最低价",
        "成交最高价",
        "成交金额",
        "全价成交金额",
        "成交金额(RMB)",
        "成交时间",
        "委托序号",
        "币种",
        "委托界面类型",
        "交易市场",
        "交易平台",
        "印花税",
        "过户费",
        "佣金",
        "结算费",
        "经手费",
        "证管费",
        "其他费",
        "总费用",
        "交易费",
        "对手方交易员代码",
        "股东代码",
        "资金账号",
        "证券类别",
        "转入证券代码",
        "转入证券名称",
        "是否匿名",
        "交易方式",
        "对手方交易主体代码",
        "回复意见",
        "对手方交易商代码",
        "对手方交易商名称",
        "对手方交易主体类型",
        "申报备注",
        "委托发起方式",
        "结算方式",
        "结算周期",
        "交收状态",
        "解除状态",
        "原成交编号",
        "处置标识",
        "违约宽限期",
        "约定号",
        "组合编号",
        "回购天数",
        "是否注册制",
        "是否尚未盈利",
        "是否具有协议控制架构",
        "是否存在表决权差异",
        "跨市场席位",
        "跨市场股东",
        "资金账号名称",
        "算法供应商",
        "策略类型",
        "清算金额",
        "清算金额（全价）"
    ],
    "资产查询": [
        "日期",
        "产品",
        "总资产",
        "净资产",
        "当日净值增长率(%)",
        "期初投入",
        "产品份额",
        "现金资产",
        "证券市值",
        "收益率(%)",
        "单位净值",
        "总资产/净资产（%）",
        "证券资产/净资产(%)",
        "证券资产/总资产(%)",
        "现金资产/净资产(%)",
        "现金资产/总资产(%)",
        "当日证券资产增长率(%)",
        "浮动盈亏",
        "累计实现盈亏",
        "总盈亏",
        "股票资产",
        "债券资产",
        "回购资产",
        "期货合约价值（非轧差）/产品总资产(%)",
        "期货账户权益/产品总资产(%)",
        "单位分红金额",
        "累计单位分红金额",
        "当日单位净值增长率(%)",
        "产品代码",
        "持仓市值(轧差)/产品净值(%)",
        "债券资产(全价)",
        "期货可用保证金",
        "期货持仓占用保证金(不含挂单)",
        "场外基金成本",
        "当日平仓收益",
        "股票浮动盈亏",
        "债券浮动盈亏",
        "基金浮动盈亏",
        "管理模式",
        "当日股票总盈亏",
        "当日债券总盈亏",
        "期货保证金账户余额",
        "期货多头资产",
        "期货空头资产",
        "股票成本",
        "债券成本",
        "基金成本",
        "当日股票收益率(%)",
        "当日债券收益率(%)",
        "期货多头资产/产品净值(%)",
        "期货空头资产/产品净值(%)",
        "产品编号",
        "产品序号",
        "沪深T+0交收业务交易可用",
        "沪深T+1交收业务交易可用",
        "银行间T+0交收业务交易可用",
        "银行间T+1交收业务交易可用"
    ]
}

T0, T1, T2, T3, T4 = '2030-03-05', '2030-03-06', '2030-03-07', '2030-03-08', '2030-03-11'
HK_FX = D('0.9')
HK = '港股通(沪)'
FUND = {'0001': '合成一号', '0002': '合成二号', '0003': '合成三号', '0004': '合成四号'}
# Identifier cells carry sentinels so a check can prove none of them reaches any output.
SENTINELS = {'股东代码': 'SENTINELGD', '资金账号': 'SENTINELZJ', '交易员姓名': 'SENTINELTR', '交易员编号': 'SENTINELTN',
             '资金账号名称': 'SENTINELZM', '算法供应商': 'SENTINELAL', '产品编号': 'SENTINELCB', '产品代码': 'SENTINELCD',
             '组合编号': 'SENTINELZH'}


def pos(name, code, close, chg, cost, units, market='上海', buy=None, sell=None, cost_fx='1', cls='股票'):
    """units maps a unit id to (opening qty, closing qty); buy and sell are (qty, average local price)."""
    return dict(name=name, code=code, close=D(close), chg=D(chg), cost=D(cost), units=units, market=market,
                buy=buy, sell=sell, cost_fx=D(cost_fx), cls=cls)


def fill(name, code, side, qty, price, fee, unit='0101', market='上海', no='混合', at='混合', cls='股票'):
    return dict(name=name, code=code, side=side, qty=D(qty), price=D(price), fee=D(fee), unit=unit, market=market, no=no, at=at, cls=cls)


def acct(total, net, cash, stock, security, shares, dividend='0'):
    return {k: D(v) for k, v in dict(total=total, net=net, cash=cash, stock=stock, security=security, shares=shares, dividend=dividend).items()}


ONE_D1 = [
    pos('合成甲', '990001', '16', '-3.03', '1000000', {'0101': (30000, 30000), '0102': (20000, 20000)}),
    pos('合成乙', '990002', '20', '-1.00', '662983.43', {'0101': (30000, 30000)}, market='深圳'),
    pos('合成丙', '09901', '25', '-2.00', '549450.55', {'0101': (20000, 20000)}, market=HK, cost_fx='0.92'),
    pos('合成丁', '990004', '30', '1.00', '-50000', {'0101': (10000, 10000)}),
    pos('合成戊', '990005', '10', '2.04', '98050', {'0101': (0, 10000)}, buy=('10000', '9.8')),
    pos('合成己', '990006', '15', '0.67', '270000', {'0101': (20000, 15000)}, sell=('5000', '14.9')),
    pos('合成庚', '990007', '50', '1.01', '280000', {'0101': (8000, 8000)}),
    pos('合成辛', '990008', '100', '0', '100000', {'0101': (1000, 1000)}, cls='交易型货币基金'),
    pos('合成壬', '990009', '8', '0', '130000', {'0101': (10000, 13000)}),
    pos('合成申', '09902', '41', '2.50', '270000', {'0101': (5000, 6000)}, market=HK, buy=('1000', '40'), cost_fx='0.92'),
    pos('合成子', '990011', '10', '0', '51136.36', {'0101': (4500, 4500)}),
    pos('合成丑', '990012', '10', '0', '46511.63', {'0101': (4000, 4000)}),
    pos('合成寅', '990013', '10', '0', '41666.67', {'0101': (3500, 3500)}),
    pos('合成卯', '990014', '10', '0', '36585.37', {'0101': (3000, 3000)}),
    pos('合成辰', '990015', '10', '0', '32051.28', {'0101': (2500, 2500)}),
    pos('合成巳', '990016', '10', '0', '28571.43', {'0101': (2000, 2000)}),
]
ONE_D1_FILLS = [
    fill('合成戊', '990005', '买入', '10000', '9.8', '50', no='20300308', at='09:31:00'),
    fill('合成申', '09902', '买入', '1000', '40', '30', market=HK),
    fill('合成己', '990006', '卖出', '5000', '14.9', '40'),
    fill('合成午', '990017', '卖出', '6000', '11', '30'),
    fill('合成辛', '990008', '买入', '6000', '100', '0', cls='交易型货币基金'),  # a large fund trade must not lead the page
]
ONE_D2 = [
    pos('合成甲', '990001', '15.5', '-3.13', '300000', {'0101': (30000, 10000), '0102': (20000, 5000)}, sell=('35000', '15.8')),
    pos('合成乙', '990002', '19.5', '-2.50', '662983.43', {'0101': (30000, 30000)}, market='深圳'),
    pos('合成丙', '09901', '25', '0', '549450.55', {'0101': (20000, 20000)}, market=HK, cost_fx='0.92'),
    pos('合成丁', '990004', '30', '0', '-50000', {'0101': (10000, 10000)}),
    pos('合成戊', '990005', '10.2', '2.00', '98050', {'0101': (10000, 10000)}),
    pos('合成己', '990006', '17.5', '16.67', '270000', {'0101': (15000, 15000)}),
    pos('合成庚', '990007', '44', '-12.00', '280000', {'0101': (8000, 8000)}),
    pos('合成辛', '990008', '100', '0', '100000', {'0101': (1000, 1000)}, cls='交易型货币基金'),
    pos('合成壬', '990009', '8', '0', '130000', {'0101': (13000, 13000)}),
    pos('合成申', '09902', '41', '0', '270000', {'0101': (6000, 6000)}, market=HK, cost_fx='0.92'),
    pos('合成丑', '990012', '10', '0', '46511.63', {'0101': (4000, 4000)}),
    pos('合成寅', '990013', '10', '0', '41666.67', {'0101': (3500, 3500)}),
    pos('合成卯', '990014', '10', '0', '36585.37', {'0101': (3000, 3000)}),
    pos('合成辰', '990015', '10', '0', '32051.28', {'0101': (2500, 2500)}),
    pos('合成巳', '990016', '9.4', '-6.00', '28571.43', {'0101': (2000, 2000)}),
]
ONE_D2_FILLS = [
    fill('合成甲', '990001', '卖出', '20000', '15.8', '150'),
    fill('合成甲', '990001', '卖出', '15000', '15.8', '120', unit='0102'),
    fill('合成子', '990011', '卖出', '4500', '9.9', '30'),
]
TWO = [
    pos('合成一', '990021', '20', '-9.09', '1200000', {'0101': (40000, 40000)}),
    pos('合成二', '990022', '20', '-5.00', '360000', {'0101': (30000, 30000)}),
    pos('合成三', '990023', '10', '-2.00', '390000', {'0101': (30000, 30000)}, market='北交所'),
]
TWO_D2 = [dict(p, chg=D(0)) for p in TWO]
FOUR = [
    pos('合成星', '990031', '11', '10.00', '147000', {'0101': (10000, 15000)}, buy=('5000', '10')),
    pos('合成月', '09903', '20', '-4.76', '176000', {'0101': (10000, 8000)}, market=HK, sell=('2000', '21'), cost_fx='0.92'),
    pos('合成日', '990033', '10', '5.00', '150000', {'0101': (20000, 20000)}, market='深圳'),
]
FOUR_D2 = [dict(p, chg=D(0), buy=None, sell=None, units={'0101': (v[1], v[1]) for v in p['units'].values()}) for p in FOUR]
FOUR_FILLS = [fill('合成星', '990031', '买入', '5000', '10', '20'), fill('合成月', '09903', '卖出', '2000', '21', '10', market=HK),
              fill('合成云', '990034', '卖出', '1000', '50', '5')]
ASSETS = {
    '0001': {T0: acct(5062500, 5062500, 1562500, 3400000, 3500000, 4050000), T1: acct(5143500, 5143500, 1643500, 3400000, 3500000, 4050000),
             T2: acct(5103000, 5103000, 1603000, 3400000, 3500000, 4050000), T3: acct(5000000, 5000000, 1504600, 3395400, 3495400, 4000000),
             T4: acct(4960050, 4960050, 2101850, 2758200, 2858200, 4000000)},
    '0002': {T0: acct(2240000, 1840000, 300000, 1940000, 1940000, 1000000), T1: acct(2180000, 1780000, 300000, 1880000, 1880000, 1000000),
             T2: acct(2100000, 1700000, 300000, 1800000, 1800000, 1000000), T3: acct(2000000, 1600000, 300000, 1700000, 1700000, 1000000),
             T4: acct(2000000, 1600000, 300000, 1700000, 1700000, 1000000)},
    '0003': {d: acct(1000000, 1000000, 1000000, 0, 0, 1000000) for d in (T0, T1, T2, T3, T4)},
    '0004': {T0: acct(980000, 980000, 500000, 480000, 480000, 1000000), T1: acct(990000, 990000, 500000, 490000, 490000, 1000000),
             T2: acct(1000000, 1000000, 470524, 529476, 529476, 1000000), T3: acct(1020000, 1020000, 511000, 509000, 509000, 1000000),
             T4: acct(1020000, 1020000, 511000, 509000, 509000, 1000000)},
}
DAYS = {
    'd1': dict(date=T3, assets=(T0, T1, T2, T3), positions={'0001': ONE_D1, '0002': TWO, '0004': FOUR}, fills={'0001': ONE_D1_FILLS, '0004': FOUR_FILLS}),
    'd2': dict(date=T4, assets=(T3, T4), positions={'0001': ONE_D2, '0002': TWO_D2, '0004': FOUR_D2}, fills={'0001': ONE_D2_FILLS}),
}


def col_name(i):
    text = ''
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        text = chr(65 + r) + text
    return text


def write_xlsx(path, sheet, header, rows):
    """Minimal workbook: one sheet, inline strings for text and plain numbers for Decimal cells."""
    def cell(c, r, value):
        ref = f'{col_name(c)}{r}'
        if value is None or value == '':
            return f'<c r="{ref}"/>'
        if isinstance(value, D):
            return f'<c r="{ref}"><v>{value}</v></c>'
        return f'<c r="{ref}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>'
    lines = []
    for r, row in enumerate([{h: h for h in header}] + rows, 1):
        lines.append(f'<row r="{r}">' + ''.join(cell(c, r, row.get(h)) for c, h in enumerate(header)) + '</row>')
    xml = ('<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
           + ''.join(lines) + '</sheetData></worksheet>')
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        z.writestr('_rels/.rels', '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr('xl/workbook.xml', f'<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="{sheet}" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels', '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr('xl/worksheets/sheet1.xml', xml)


def position_rows(date, fid, items, total, net):
    rows = []
    for p in items:
        whole = sum(c for _, c in p['units'].values())
        hk = p['market'] == HK
        fx = HK_FX if hk else D(1)
        for unit, (opening, closing) in p['units'].items():
            share = D(closing) / D(whole)
            mv = D(closing) * p['close']
            cost = p['cost'] * share
            pnl = mv - cost
            buy_q = D(closing - opening) if p['buy'] else D(0)
            sell_q = D(opening - closing) if p['sell'] else D(0)
            buy_p = D(p['buy'][1]) if p['buy'] else D(0)
            sell_p = D(p['sell'][1]) if p['sell'] else D(0)
            row = {h: 'NaN' for h in ('盯市盈亏', '保本价')}
            row.update({'日期': date, '产品': FUND[fid], '产品序号': fid, '单元编号': unit, '单元': FUND[fid] + '-' + unit, '交易市场': p['market'],
                        '证券代码': p['code'], '证券名称': p['name'], '持仓市值（本币）': mv, '持仓市值(人民币)': mv * fx, '持仓市值(净价)': mv * fx, '持仓市值(全价)': mv * fx,
                        '市值/总资产(%)': mv * fx * 100 / total, '市值/净资产(%)': mv * fx * 100 / net, '最新价': p['close'], '持仓数量': D(closing), '期初数量': D(opening),
                        '可用': D(opening), '当日涨跌幅（%）': p['chg'], '当前成本（全价本币）': cost, '当前成本（全价人民币）': cost * p['cost_fx'], '当前成本(净价)': cost,
                        '成本价（全价本币）': cost / D(closing), '成本价（全价人民币）': cost * p['cost_fx'] / D(closing), '成本价(净价)': cost / D(closing), '当日变动数量': D(closing - opening),
                        '浮动盈亏': pnl, '浮动盈亏（人民币）': mv * fx - cost * p['cost_fx'], '实现盈亏': D(0), '累计盈亏': pnl, '浮盈率（%）': pnl * 100 / mv,
                        '损益率（%）': pnl * 100 / cost, '总体盈亏率(%)': pnl * 100 / cost, '多空标志': '多仓', '币种': '港币' if hk else '人民币',
                        '证券类别': p['cls'], '资产类别': '股票资产' if p['cls'] == '股票' else '基金资产', '停牌标志': '正常交易',
                        '当日买入数量': buy_q, '当日买入均价': buy_p, '当日卖出数量': sell_q, '当日卖出均价': sell_p, '当日买金额': buy_q * buy_p, '当日卖金额': sell_q * sell_p,
                        '股东代码': SENTINELS['股东代码'] + fid, '资金账号': SENTINELS['资金账号'] + fid, '产品编号': SENTINELS['产品编号'] + fid, '投资类型': '可交易/FVPL'})
            rows.append(row)
    return rows


def fill_rows(date, fid, items):
    rows = []
    for f in items:
        hk = f['market'] == HK
        fx = HK_FX if hk else D(1)
        amount = f['qty'] * f['price']
        rows.append({'成交编号': f['no'], '证券代码[委托方向]': f"{f['code']}[{f['side']}]", '证券代码': f['code'], '单元': FUND[fid] + '-' + f['unit'], '产品序号': fid, '产品': FUND[fid],
                     '发生日期': date, '证券名称': f['name'], '委托方向': f['side'], '成交数量': f['qty'], '成交均价': f['price'], '全价成交均价': 'NaN', '成交金额': amount,
                     '全价成交金额': 'NaN', '成交金额(RMB)': amount * fx, '成交时间': f['at'], '委托序号': '混合', '币种': '港币' if hk else '人民币', '交易市场': f['market'],
                     '总费用': f['fee'], '其他费': D(0), '清算金额': amount * fx + (f['fee'] if f['side'] == '买入' else -f['fee']), '证券类别': f['cls'],
                     '交易员编号': SENTINELS['交易员编号'], '交易员姓名': SENTINELS['交易员姓名'], '股东代码': SENTINELS['股东代码'] + fid, '资金账号': SENTINELS['资金账号'] + fid,
                     '资金账号名称': SENTINELS['资金账号名称'], '算法供应商': SENTINELS['算法供应商'], '组合编号': SENTINELS['组合编号']})
    return rows


def asset_rows(dates):
    rows = []
    for date in dates:
        for fid in FUND:
            days = sorted(ASSETS[fid])
            a = ASSETS[fid][date]
            nav = a['net'] / a['shares']
            i = days.index(date)
            prev = ASSETS[fid][days[i - 1]] if i else None
            unit_growth = (nav / (prev['net'] / prev['shares']) - 1) * 100 if prev else D(0)
            net_growth = (a['net'] / prev['net'] - 1) * 100 if prev else D(0)
            rows.append({'日期': date, '产品': FUND[fid], '产品序号': fid, '总资产': a['total'], '净资产': a['net'], '现金资产': a['cash'], '证券市值': a['security'], '股票资产': a['stock'],
                         '产品份额': a['shares'], '单位净值': nav, '当日单位净值增长率(%)': unit_growth, '当日净值增长率(%)': net_growth, '单位分红金额': a['dividend'], '累计单位分红金额': a['dividend'],
                         '证券资产/总资产(%)': a['security'] * 100 / a['total'], '现金资产/总资产(%)': a['cash'] * 100 / a['total'], '产品代码': SENTINELS['产品代码'] + fid,
                         '产品编号': SENTINELS['产品编号'] + fid, '沪深T+0交收业务交易可用': 'NaN', '沪深T+1交收业务交易可用': 'NaN'})
    last = sum((ASSETS[f][dates[-1]]['total'] for f in FUND), D(0))
    rows.append({'日期': '汇总 ', '总资产': last, '单位净值': D(1)})  # the real label carries a trailing space
    return rows


def build_day(folder, spec):
    folder.mkdir(parents=True, exist_ok=True)
    date = spec['date']
    held = []
    for fid, items in spec['positions'].items():
        a = ASSETS[fid][date]
        held += position_rows(date, fid, items, a['total'], a['net'])
    traded = []
    for fid, items in spec['fills'].items():
        traded += fill_rows(date, fid, items)
    write_xlsx(folder / '各产品持仓.xlsx', '持仓查询', HEADERS['各产品持仓'], held)
    write_xlsx(folder / '成交查询.xlsx', '成交查询', HEADERS['成交查询'], traded)
    write_xlsx(folder / '资产查询.xlsx', '产品资产查询', HEADERS['资产查询'], asset_rows(spec['assets']))
    (folder / 'SYNTHETIC.txt').write_text('全合成样例，名称、代码和数字都是虚构的。\n', encoding='utf-8')


def build(out):
    for name, spec in DAYS.items():
        build_day(Path(out) / name, spec)


def build_many(out):
    """Write the 36-product fictional base, retaining the original fixture cells."""
    folder = Path(out)
    if folder.exists() and (not folder.is_dir() or any(folder.iterdir())):
        raise ValueError('fixture output must be empty')
    spec = DAYS['d1']
    held, traded = [], []
    for fid, items in spec['positions'].items():
        a = ASSETS[fid][spec['date']]
        held += position_rows(spec['date'], fid, items, a['total'], a['net'])
    for fid, items in spec['fills'].items():
        traded += fill_rows(spec['date'], fid, items)
    sources = ['0001'] * 12 + ['0002'] * 10 + ['0004'] * 12 + ['0003'] * 2
    own_codes = {'990011', '990012', '990013', '990014', '990015', '990016', '990023', '990033'}
    seen, plan = {}, []
    for n, src in enumerate(sources, 1):
        copy = seen.get(src, 0)
        plan.append((f'{n:04d}', src, copy))
        seen[src] = copy + 1
    hidden = next(fid for fid, src, copy in reversed(plan) if src == '0004')
    folder.mkdir(parents=True, exist_ok=True)
    for name, sheet, rows in (('各产品持仓', '持仓查询', held), ('成交查询', '成交查询', traded),
                              ('资产查询', '产品资产查询', asset_rows(spec['assets']))):
        widened = []
        for fid, src, copy in plan:
            for row in rows:
                if row.get('产品序号') != src:
                    continue
                row = dict(row)
                row['产品序号'], row['产品'] = fid, f'合成产品{fid}'
                if row.get('单元'):
                    row['单元'] = row['单元'].replace(FUND[src], row['产品'])
                code = row.get('证券代码')
                if code:
                    k = 50 + copy if code in own_codes else copy // 2
                    row['证券代码'] = code[:1] + f'{k:02d}' + code[3:]
                if name == '资产查询' and fid == hidden:
                    if row['日期'] != spec['date']:
                        row['现金资产'] = D(1000)
                    row['总资产'] = row['净资产'] = row['股票资产']
                    row['单位净值'] = row['净资产'] / row['产品份额']
                widened.append(row)
        widened += [row for row in rows if not row.get('产品序号')]
        write_xlsx(folder / f'{name}.xlsx', sheet, HEADERS[name], widened)
    (folder / 'SYNTHETIC.txt').write_text('全合成样例，名称、代码和数字都是虚构的。\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True)
    parser.add_argument('--many', action='store_true', help='write one export with 36 fictional products')
    args = parser.parse_args()
    if args.many:
        try:
            build_many(args.out)
        except (ValueError, OSError) as err:
            parser.exit(1, f'REJECTED: {err}\n')
    else:
        build(args.out)


if __name__ == '__main__':
    main()
