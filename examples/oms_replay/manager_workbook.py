"""Write the manager's editable Excel sheet from the corresponding daily CSV."""
import csv
import re
import unicodedata
from datetime import date
from decimal import Decimal
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

from render import CSV_HEADER, csv_asset_ratio


MAIN = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
OFFICE = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
PACKAGE = 'http://schemas.openxmlformats.org/package/2006/relationships'
TYPES = 'application/vnd.openxmlformats-officedocument.spreadsheetml'
WIDTHS = (16, 16, 20, 16, 16, 14, 18, 14, 19, 14, 14, 18, 14, 14, 14, 14, 14, 14, 50)
# name, number format, font, fill, wrap
STYLES = [
    ('text', 0, 0, 0, True), ('heading', 0, 1, 2, True), ('label', 0, 2, 3, True),
    ('integer', 3, 0, 0, False), ('amount', 4, 0, 0, False), ('price3', 164, 0, 0, False),
    ('nav', 165, 0, 0, False), ('percent', 166, 0, 0, False), ('change', 167, 0, 0, False),
    ('entry', 4, 0, 4, False), ('date', 168, 0, 0, False),
]
STYLE_ID = {style[0]: str(i) for i, style in enumerate(STYLES)}


def styles_xml(*, unsigned_zero=False):
    root = ET.Element('styleSheet', xmlns=MAIN)
    formats = ET.SubElement(root, 'numFmts', count='5')
    for i, code in enumerate(('0.000', '0.0000', '0.0%', '+0.00%;-0.00%;+0.00%', 'yyyy-mm-dd'), 164):
        if unsigned_zero and i==167:code='+0.00%;-0.00%;0.00%'
        ET.SubElement(formats, 'numFmt', numFmtId=str(i), formatCode=code)
    fonts = ET.SubElement(root, 'fonts', count='3')
    for color, bold in [('FF20334A', False), ('FFFFFFFF', True), ('FF17344D', True)]:
        font = ET.SubElement(fonts, 'font')
        ET.SubElement(font, 'name', val='Microsoft YaHei')
        if bold:
            ET.SubElement(font, 'b')
        ET.SubElement(font, 'color', rgb=color)
        ET.SubElement(font, 'sz', val='10')
    fills = ET.SubElement(root, 'fills', count='5')
    for pattern, color in [('none', ''), ('gray125', ''), ('solid', 'FF264965'),
                           ('solid', 'FFE8F0F4'), ('solid', 'FFFFE6A9')]:
        fill = ET.SubElement(ET.SubElement(fills, 'fill'), 'patternFill', patternType=pattern)
        if color:
            ET.SubElement(fill, 'fgColor', rgb=color)
            ET.SubElement(fill, 'bgColor', indexed='64')
    ET.SubElement(ET.SubElement(root, 'borders', count='1'), 'border')
    base = dict(numFmtId='0', fontId='0', fillId='0', borderId='0')
    ET.SubElement(ET.SubElement(root, 'cellStyleXfs', count='1'), 'xf', base)
    xfs = ET.SubElement(root, 'cellXfs', count=str(len(STYLES)))
    for _, fmt, font, fill, wrap in STYLES:
        xf = ET.SubElement(xfs, 'xf', numFmtId=str(fmt), fontId=str(font), fillId=str(fill), borderId='0',
                           xfId='0', applyNumberFormat='1', applyFill='1', applyFont='1', applyAlignment='1')
        ET.SubElement(xf, 'alignment', vertical='center', wrapText='1' if wrap else '0')
    ET.SubElement(ET.SubElement(root, 'cellStyles', count='1'), 'cellStyle', name='Normal', xfId='0', builtinId='0')
    return root


def number(raw):
    if not re.fullmatch(r'[+-]?[0-9]+(?:\.[0-9]+)?', raw) or len(raw) > 308:
        raise ValueError('invalid worksheet number')
    return Decimal(raw)


def formula_cells(rows, header, total):
    """Only formulas at these addresses may execute. Row numbers here are one-based."""
    a = total + 8  # row shared by opening assets, ending assets, NAV and editable debt
    cells = {}
    def add(row, column, expected, value, style='amount', excel=None):
        if rows[row - 1][column] != expected:
            raise ValueError('unexpected holdings formula')
        cells[row, column] = (excel or expected[1:], value, style)

    def amount(row, col=8):
        return number(rows[row - 1][col] or '0')

    nav = re.fullmatch(rf'=E{a}/([0-9]+(?:\.[0-9]+)?)', rows[a - 1][8])
    if not nav or number(nav[1]) <= 0:
        raise ValueError('invalid NAV formula')
    outside = amount(total + 2) + amount(total + 3)
    cash = amount(total + 5) + amount(total + 6) + outside
    end = amount(total) + cash - amount(a, 11)
    pnl = end - amount(a, 2)
    add(total + 4, 8, f'=SUM(I{total+2}:I{total+3})', outside)
    add(total + 7, 8, f'=I{total+5}+I{total+6}+I{total+4}', cash)
    add(a, 4, f'=I{total}+I{total+7}-L{a}', end)
    add(a, 8, nav[0], end / number(nav[1]), 'nav')
    for row in range(a, a + 3):
        add(row, 6, f'=E{a}-C{a}', pnl)
    for row in [*range(header + 1, total - 1), total, total + 5, total + 7]:
        mv = cash if row == total + 7 else amount(row)
        add(row, 9, csv_asset_ratio(f'I{row}', f'E{a}'), mv / end if end else 0, 'percent',
            f'IF(E{a}=0,0,I{row}/E{a})')
    return cells


def text_width(value):
    return sum(2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in value)


def worksheet(rows, *, nav_start=5, table_rows=(), state_span=False, nav_count=3, nav_cumulative=False):
    if not 4 <= len(rows) <= 1048576 or any(len(row) != len(CSV_HEADER) for row in rows):
        raise ValueError('invalid holdings sheet')
    if not rows[0][0] or rows[1][0] != '基金持仓' or rows[2][0] != '日期' or rows[3][0] != '产品概览':
        raise ValueError('invalid holdings title')
    rejected = len(rows) == 4 and rows[3][1].startswith('今日没有持仓表：')
    header, total = 0, 0
    formulas = {}
    if not rejected:
        header = next((i for i, row in enumerate(rows, 1) if row == CSV_HEADER), 0)
        total = len(rows) - 14
        if header < 8 or total < header + 2 or rows[total-1][1] != '持仓总计' or rows[total+7][3] != '期末资产':
            raise ValueError('invalid holdings layout')
        formulas = formula_cells(rows, header, total)

    root = ET.Element('worksheet', xmlns=MAIN)
    cols = ET.SubElement(root, 'cols')
    for c, width in enumerate(WIDTHS, 1):
        ET.SubElement(cols, 'col', min=str(c), max=str(c), width=str(width), customWidth='1')
    data = ET.SubElement(root, 'sheetData')
    merges = []
    for i, row in enumerate(rows, 1):
        height = 22
        label, sub = row[:2]
        if i in (1, 2):
            merges.append(f'A{i}:H{i}')
        elif i == 4 or (4 < i < header and i not in range(nav_start, nav_start + nav_count) and i not in table_rows and label):
            merges.append(f'B{i}:H{i}')
            height = max(24, (1 + text_width(sub) // 95) * 18, (1 + text_width(label) // WIDTHS[0]) * 18)
        elif i in table_rows:
            if state_span:
                if row[3] or row[4]:raise ValueError('state span would hide cells')
                merges.append(f'C{i}:E{i}')
            height = max(36, max((1 + text_width(value) // WIDTHS[j]) * 18 for j, value in enumerate(row)))
        elif total and i >= total + 11:
            merges.append(f'E{i}:K{i}')
        elif header < i < total - 1:
            height = max(height, (1 + text_width(row[18]) // 45) * 18)
        dest = ET.SubElement(data, 'row', r=str(i), ht=str(min(height, 409)), customHeight='1')
        for j, raw in enumerate(row):
            editable = bool(total and ((j == 8 and i in (total + 2, total + 3)) or (i == total + 8 and j == 11)))
            if not raw and not editable:
                continue
            style = 'text'
            value = raw
            formula = None
            if (i, j) in formulas:
                formula, value, style = formulas[i, j]
            elif editable:
                value, style = (number(raw) if raw else None), 'entry'
            elif nav_cumulative and nav_start <= i < nav_start+nav_count and j >= 2 and raw in ('5 日累计','—'):
                style='label' if raw=='5 日累计' else 'text'
            elif (i == 3 and j == 1) or (i == nav_start and j >= 2):
                value, style = (date.fromisoformat(raw) - date(1899, 12, 30)).days, 'date'
            elif i == nav_start + 1 and j >= 2:
                value, style = number(raw), 'nav'
            elif nav_start+2 <= i < nav_start+nav_count and j >= 2 and raw != '未知':
                if not raw.endswith('%'):
                    raise ValueError('invalid daily return')
                value, style = number(raw[:-1]) / 100, ('percent' if nav_cumulative and sub=='股票仓位' else 'change')
            elif header and header < i < total - 1 and j not in (1, 2, 3, 13, 18):
                if j in (10, 12):
                    if not raw.endswith('%'):
                        raise ValueError('invalid holdings percentage')
                    value, style = number(raw[:-1]) / 100, 'percent'
                else:
                    value = number(raw)
                    if j in (0, 5, 14, 16):
                        style = 'integer'
                    elif j in (4, 7, 15, 17) and len(raw.partition('.')[2]) == 3:
                        style = 'price3'
                    else:
                        style = 'amount'
            elif total and ((j == 8 and i in (total, total + 5, total + 6)) or (i == total + 8 and j == 2)):
                value, style = number(raw), 'amount'
            elif i in (1, 2, header):
                style = 'heading'
            elif j == 0 or i in (4, total + 1, total + 8):
                style = 'label'
            addr = chr(65 + j) + str(i)
            cell = ET.SubElement(dest, 'c', r=addr, s=STYLE_ID[style])
            if formula is not None:
                ET.SubElement(cell, 'f').text = formula
            if isinstance(value, str):
                if len(value) > 32767 or re.search(r'[\x00-\x08\x0b-\x0c\x0e-\x1f]', value):
                    raise ValueError('unsupported Excel text')
                cell.set('t', 'inlineStr')
                text = ET.SubElement(ET.SubElement(cell, 'is'), 't')
                text.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
                text.text = re.sub(r'_(?=x[0-9A-Fa-f]{4}_)', '_x005F_', value)
            elif value is not None:
                ET.SubElement(cell, 'v').text = str(value)
    merged = ET.SubElement(root, 'mergeCells', count=str(len(merges)))
    for ref in merges:
        ET.SubElement(merged, 'mergeCell', ref=ref)
    return root


# Static reading guide travels with each new homepage; old callers keep one sheet.
EXPLANATIONS = (
    ('首页怎么读', '先看近 5 日表和今日结论，再看重点持仓和今日成交。逐只完整提醒在下方风控提示列。'),
    ('今日结论', '只写近 5 日表里没有的状态和判断，依次是：净值距高点的回撤和所在的警示线；与沪深300 的差额（净值在回撤提醒中比高点以来，其他时候比近 5 日）；浮亏超 10% 的持仓只数和合计占总资产的比例，合计达到 20% 写“浮亏面大”（试用值）；今日升级、临近下一条警示线、提醒解除的只数；今日净买入和换手，净值在回撤提醒中时再写首次提醒日之后的累计净买入。净买入＝（买入金额−卖出金额）÷当日总资产；换手＝（买入金额＋卖出金额）÷当日总资产。今日涨跌、仓位和指数涨跌看近 5 日表，证券名看重点持仓和今日成交。'),
    ('净值回撤警示线','净值距高点严格超过 5%、10%、15%、20% 时分别提醒；恰好等于警示线数值不算超过。'),
    ('浮亏警示线','持仓浮亏严格超过 10%、15%、20%、30% 时分别提醒。'),
    ('近 5 日跌幅警示线','同一段持仓最近 5 个估值日的涨跌连乘，跌幅严格超过 10% 时提醒；不足 5 日不据此判定。'),
    ('盈利保护警示线','本段持仓曾浮盈严格超过 30% 后，较本段已记录最高收盘价回撤严格超过 10% 时提醒。账本之前的最高价未知，只从有记录的持仓开始计算。'),
    ('提醒解除','反弹一天不解除。连续 5 个已确认估值日回到最低警示线以内才解除；期间缺数据，连续日数重新计算。清仓结束该持仓提醒。'),
    ('净值高点','取最近 60 个估值日的最高净值；不足 60 日时取现有净值记录，页面一律写高点。提醒段内累计的高点基准固定，不随滚动窗口移动。'),
    ('提醒开始日','首次超过警示线的日期是本段提醒开始日。账本首日已经在警示线外时，只知道有记录以来在警示线外，不能认定当天首次超过警示线。'),
    ('提醒以来股价','从提醒开始日之后到今日逐日涨跌连乘；提醒开始日当天用破折号，不重复今日仓位。'),
    ('日贡献累计','把区间内各日持仓盈亏占前一日净资产的百分点直接相加。它不是投资收益率，清仓后缺少的股价不补造。'),
    ('提醒以来操作','每日买入金额减卖出金额，除以当日总资产后逐日累加，单位为百分点。买、卖日数分别数有该方向成交的日子；同日双向两边都计一天。'),
    ('回撤期间操作','从本净值段首次提醒日的下一估值日起累计，分别列净买入、净卖出最多的三只。零不列；名单不可得或两侧都空时省略。已提醒持仓按成交前一估值日收盘状态认定。'),
    ('今日成交','同一证券同方向按当日成交金额除成交数量计算均价。均价与收盘偏离达到 3% 才标方向和幅度；清仓缺收盘价时不标。前日缺导出时写当日成交百分点及收盘结果。'),
    ('重点持仓','依次列今日升级、今日成交、临近下一条警示线、拖累最大；首日按仓位乘浮亏程度排序，写首日已在警示线外。其余提醒不超过 3 只全部入表，更多则逐只列名。'),
    ('5 日累计','五个当日涨跌的累计需要六个净值端点；缺起点或期间分红导致不可比时写未知。单位净值和股票仓位不累计。'),
    ('缺数据','缺导出或区间端点写未知；有数据但成交未核对写待核实；起点当天不计算的累计写破折号。数据覆盖写明日期、缺口和受影响的行；未提供指数则省略指数项。'),
    ('净值连跌','净值连续下跌至少 3 个估值日时，今日结论写“净值连跌 N 日”（试用条件）。'),
    ('多只','至少 3 只。'),
    ('回撤提醒后的增减仓','首次提醒日之后累计净买入达到 2 个百分点，并且股票仓位比首次提醒日高出至少 2 个百分点，写不降反升；累计净卖出达到 2 个百分点，并且股票仓位低至少 2 个百分点，写已减仓；其余写未减仓。净买卖达到 2 个百分点而仓位没有同向变化这么多时（常见于申购后补仓、赎回后卖出），同一句写出股票仓位的前后值。缺数据不下这项结论。均为试用条件。'),
    ('临近下一条警示线','持仓仍在提醒段内，距尚未越过的最低浮亏警示线不超过 1 个百分点；恰好在警示线上距离为零。'),
    ('注意风险', '以下三项成立两项及以上时写在今日结论末尾：净值在回撤提醒中（不含回到警示线内的观察期）；浮亏超 10% 的持仓合计占总资产达到 20%；股票仓位达到 80%，且净值在回撤提醒中时没有已减仓的认定。这是风险归纳，不是交易指令。'),
    ('表尾手工金额','黄色金额格可填场外金额和负债，Excel 会重算表尾。首页使用 OMS 收盘导出的数据，不因表尾手填金额重新分析。'),
    ('重跑与新版本','相同导出、首页内容、指数和程序重跑保留已编辑工作簿；任一变化另出一版。旧手改保留在原版本，新版需核对并重新填写。'),
    ('追涨杀跌怎么认','按一只证券、一天、一个方向的成交合计，以成交金额÷成交数量算均价。买入均价较昨收达到 +X%，或高于昨收且高于收盘至少 3%，记追涨；卖出较昨收达到 -X%，或低于昨收且低于收盘至少 3%，记杀跌。其余买入均价高于收盘至少 3%记买入后续跌；卖出均价低于收盘至少 3%记卖出后续涨。沪深普通档 X=5；创业板30、上海68、北交所、港股通 X=7；其他市场不判。缺昨收/收盘、成交未核对，或当日涨跌绝对值超过10.5%/20.5%/30.5%（普通/创业科创/北交所）不判，港股通不限涨跌幅。日线成交日为除权除息日时，昨收不可比，不判；前一交易日除权不影响判定。仍持有的用持仓导出价格；日线只取有已核对成交且收盘已不持有的证券，用不复权的当日与前一交易日收盘。当天沪深15:30、港股通16:30前不取；历史成交日不限。没有接口的市场不取，取数失败不挡生成。汇总按人民币成交额计算；这是单日成交描述，不据此判断习惯。这是按日线涨跌幅做的粗略估计，没有用每笔成交的时间。'),
    ('旧版回退','标题注明新版首页生成失败、本页为旧版时，本产品显示旧版首页。其他产品照常生成新版；数据校验或写盘失败会停止，不借回退掩盖错误。'),
    ('行为评估怎么读', '第三张表“行为评估”看最近 20 个有账本的交易日，账本第一天没有前一日，不计入。每行分开写当时的操作和之后的结果：两样都成立写“缺陷”，操作成立、已知部分未见不利结果写“特征”，操作不成立写“未见”；没到下结论的条件写“样本不足”，窗口内没有出现这种情形写“未触发”，缺少下结论必需的数据写“待核实”。不凭盈亏推定动机。门槛都是试用值。'),
    ('回撤提醒后的仓位', '按净值的每一段回撤提醒写一句。不降反升、已减仓、未减仓的认定与首页“回撤提醒后的增减仓”相同。之后升到更高的净值回撤警示线，或回撤比首次提醒日再深至少 2 个百分点，算不利结果。首次提醒日之后满 5 个估值日才下结论。已减仓写“未见”。回撤算不出的日子会注明，已知的最高警示线和最深回撤照样写。'),
    ('浮亏警示后的处置', '只看浮亏警示线。持仓跌破浮亏警示线满 5 个估值日后计入，每只证券只算一次。同一证券有多段时，依次优先选操作可核实、未减仓、之后升到更高浮亏警示线、加仓的段；仍相同时取最近一段。之后这只证券净买入、并且每份基金对应的持股数量增加超过 1%，算加仓；减仓对称；清仓单列；其余算未动，包括随申购赎回的等比例买卖。未减仓并且之后升到更高浮亏警示线的持仓达到 3 只，写缺陷“浮亏扩大未处置”，其中加仓的达到 3 只再写“越跌越买”；未减仓的达到 3 只而升到更高警示线的不足 3 只，写特征。只列不为零的操作类别；全部持仓的操作都待核实时，结论写“待核实”。'),
    ('追涨杀跌（窗口）', '把“今日成交”逐日的追涨、杀跌标注在窗口内合计，一只证券、一天、一个方向算一笔。追涨和杀跌占窗口成交额达到 15%、出现在至少 5 个交易日、笔数多于反方向（低吸、高抛），算行为成立；这些成交按当日收盘价计合计为亏，算不利结果。窗口满 20 日、有成交的交易日至少 10 个、未能判定的成交额不超过三分之一才下结论。判为缺陷并且窗口内新跌破浮亏 10% 警示线的持仓达到 5 只时，加写“风险偏好过高，不适应当前市场”。'),
    ('交易频率', '日均换手超过 10%，并且日均买卖标的超过 20 只，算行为成立。换手＝（买入金额＋卖出金额）÷当日总资产，两项都按窗口内全部交易日平均。窗口累计收益不高于 0 算不利结果，同一行写累计交易费用占资产的比例。窗口满 20 日才下结论。'),
    ('行为评估不评价的', '决策依据：没有买卖理由的记录，不评价。持仓均衡、仓位管理适应市场：需要 60 个估值日的记录，暂不评价。评价单位是一个产品，不汇总同一位经理的多个产品。'),
)


def explanation_sheet():
    root=ET.Element('worksheet',xmlns=MAIN)
    cols=ET.SubElement(root,'cols')
    for c,width in ((1,25),(2,110)):ET.SubElement(cols,'col',min=str(c),max=str(c),width=str(width),customWidth='1')
    data=ET.SubElement(root,'sheetData')
    for n,(label,content) in enumerate(EXPLANATIONS,1):
        row=ET.SubElement(data,'row',r=str(n),ht=str(max(36,(1+text_width(content)//105)*18)),customHeight='1')
        for col,value,style in [('A',label,'label'),('B',content,'text')]:
            cell=ET.SubElement(row,'c',r=col+str(n),s=STYLE_ID[style],t='inlineStr')
            ET.SubElement(ET.SubElement(cell,'is'),'t').text=value
    return root


def behavior_sheet(page, style_start):
    """Four readable columns; all user-facing cells are literal strings."""
    from behavior_review import HEADER
    widths = (18, 10, 64, 48)
    root = ET.Element('worksheet', xmlns=MAIN)
    cols = ET.SubElement(root, 'cols')
    for column, width in enumerate(widths, 1):
        ET.SubElement(cols, 'col', min=str(column), max=str(column), width=str(width), customWidth='1')
    # A span is a single row merged over all columns. Examples keep the item label.
    grid = [(True, [page['head'][0]], True)]
    grid += [(False, [text], True) for text in page['head'][1:]]
    grid.append((False, [], False))
    if page['table']:
        grid.append((True, HEADER, False))
        grid += [(False, cells, False) for cells in page['table']]
        grid.append((False, [], False))
    if page['examples']:
        grid.append((True, ['例子'], True))
        grid += [(False, [cells[0], '', cells[2], ''], False) for cells in page['examples']]
        grid.append((False, [], False))
    grid += [(False, [text], True) for text in page['tail']]
    data = ET.SubElement(root, 'sheetData'); merges = []
    for n, (bold, cells, span) in enumerate(grid, 1):
        available = [sum(widths)-4] if span else [width-2 for width in widths]
        height = max([24] + [(1 + text_width(text) // int(available[i])) * 18 + 6 for i, text in enumerate(cells)])
        dest = ET.SubElement(data, 'row', r=str(n), ht=str(min(height, 409)), customHeight='1')
        if span:
            merges.append(f'A{n}:D{n}')
        for i, text in enumerate(cells):
            if not text:
                continue
            if len(text) > 32767 or re.search(r'[\x00-\x08\x0b-\x0c\x0e-\x1f]', text):
                raise ValueError('unsupported Excel text')
            c = ET.SubElement(dest, 'c', r=f'{chr(65+i)}{n}', s=str(style_start+int(bold)), t='inlineStr')
            t = ET.SubElement(ET.SubElement(c, 'is'), 't')
            t.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
            t.text = re.sub(r'_(?=x[0-9A-Fa-f]{4}_)', '_x005F_', text)
    merged = ET.SubElement(root, 'mergeCells', count=str(len(merges)))
    for ref in merges:
        ET.SubElement(merged, 'mergeCell', ref=ref)
    return root


def write_workbook(source_csv: Path, target_xlsx: Path, *, nav_start=5, table_rows=(), unsigned_zero=False, state_span=False, nav_count=3, nav_cumulative=False, explanation=False, behavior=None):
    """Write to a new target only. The manager publishes the completed set together."""
    with source_csv.open(encoding='utf-8', newline='') as src:
        sheet = worksheet(list(csv.reader(src, strict=True)), nav_start=nav_start, table_rows=table_rows, state_span=state_span, nav_count=nav_count, nav_cumulative=nav_cumulative)
    types = ET.Element('Types', xmlns='http://schemas.openxmlformats.org/package/2006/content-types')
    ET.SubElement(types, 'Default', Extension='rels', ContentType='application/vnd.openxmlformats-package.relationships+xml')
    ET.SubElement(types, 'Default', Extension='xml', ContentType='application/xml')
    for file, kind in [('workbook.xml', 'sheet.main'), ('worksheets/sheet1.xml', 'worksheet'), ('styles.xml', 'styles')]:
        ET.SubElement(types, 'Override', PartName='/xl/' + file, ContentType=f'{TYPES}.{kind}+xml')
    book = ET.Element('workbook', {'xmlns': MAIN, 'xmlns:r': OFFICE})
    ET.SubElement(ET.SubElement(book, 'sheets'), 'sheet', name='持仓表', sheetId='1', attrib={'r:id': 'rId1'})
    ET.SubElement(book, 'calcPr', calcId='191029', calcMode='auto', fullCalcOnLoad='1')
    package = ET.Element('Relationships', xmlns=PACKAGE)
    ET.SubElement(package, 'Relationship', Id='rId1', Type=OFFICE+'/officeDocument', Target='xl/workbook.xml')
    links = ET.Element('Relationships', xmlns=PACKAGE)
    ET.SubElement(links, 'Relationship', Id='rId1', Type=OFFICE+'/worksheet', Target='worksheets/sheet1.xml')
    ET.SubElement(links, 'Relationship', Id='rId2', Type=OFFICE+'/styles', Target='styles.xml')
    parts = {'[Content_Types].xml': types, '_rels/.rels': package, 'xl/workbook.xml': book,
             'xl/_rels/workbook.xml.rels': links, 'xl/styles.xml': styles_xml(unsigned_zero=unsigned_zero), 'xl/worksheets/sheet1.xml': sheet}
    if explanation:
        ET.SubElement(types,'Override',PartName='/xl/worksheets/sheet2.xml',ContentType=f'{TYPES}.worksheet+xml')
        ET.SubElement(book.find('sheets'),'sheet',name='说明',sheetId='2',attrib={'r:id':'rId3'})
        ET.SubElement(links,'Relationship',Id='rId3',Type=OFFICE+'/worksheet',Target='worksheets/sheet2.xml')
        parts['xl/worksheets/sheet2.xml']=explanation_sheet()
    if behavior is not None:
        if not explanation:
            raise ValueError('behavior review requires the reading guide')
        ET.SubElement(types,'Override',PartName='/xl/worksheets/sheet3.xml',ContentType=f'{TYPES}.worksheet+xml')
        ET.SubElement(book.find('sheets'),'sheet',name='行为评估',sheetId='3',attrib={'r:id':'rId4'})
        ET.SubElement(links,'Relationship',Id='rId4',Type=OFFICE+'/worksheet',Target='worksheets/sheet3.xml')
        styles = parts['xl/styles.xml']
        xfs = styles.find('cellXfs')
        style_start = len(xfs)
        for font, fill in ((0, 0), (1, 2)):
            xf = ET.SubElement(xfs, 'xf', numFmtId='0', fontId=str(font), fillId=str(fill), borderId='0',
                               xfId='0', applyFont='1', applyFill='1', applyAlignment='1')
            ET.SubElement(xf, 'alignment', vertical='top', wrapText='1')
        xfs.set('count', str(len(xfs)))
        parts['xl/worksheets/sheet3.xml'] = behavior_sheet(behavior, style_start)
    with target_xlsx.open('xb') as target:
        try:
            with ZipFile(target, 'w', ZIP_DEFLATED) as archive:
                for filename, part in parts.items():
                    archive.writestr(filename, ET.tostring(part, encoding='utf-8', xml_declaration=True))
        except BaseException:
            target.close()
            target_xlsx.unlink()
            raise
