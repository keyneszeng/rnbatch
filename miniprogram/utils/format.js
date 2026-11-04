const PHOTO = ['jpg', 'jpeg', 'png', 'heic', 'heif', 'gif', 'webp', 'bmp', 'tif', 'tiff'];
const DOC = ['doc', 'docx', 'pages', 'rtf', 'txt', 'md'];
const SHEET = ['xls', 'xlsx', 'csv', 'numbers'];
const SLIDE = ['ppt', 'pptx', 'key'];
const VIDEO = ['mp4', 'mov', 'avi', 'mkv'];

// 徽标只放 1-2 个字，多了在 72rpx 的方块里放不下
const LABELS = {
    照片: '照',
    文档: '文',
    表格: '表',
    演示: '演',
    视频: '视',
    PDF: 'PDF',
    压缩包: '压',
    其他: '档',
};

function extOf(name) {
    const clean = String(name || '');
    const dot = clean.lastIndexOf('.');
    if (dot <= 0 || dot === clean.length - 1) {
        return '';
    }
    return clean.slice(dot + 1).toLowerCase();
}

function typeOf(name) {
    const ext = extOf(name);
    if (PHOTO.indexOf(ext) >= 0) return '照片';
    if (VIDEO.indexOf(ext) >= 0) return '视频';
    if (ext === 'pdf') return 'PDF';
    if (DOC.indexOf(ext) >= 0) return '文档';
    if (SHEET.indexOf(ext) >= 0) return '表格';
    if (SLIDE.indexOf(ext) >= 0) return '演示';
    if (ext === 'zip' || ext === 'rar' || ext === '7z') return '压缩包';
    return '其他';
}

function badgeOf(name) {
    return LABELS[typeOf(name)] || '档';
}

function sizeText(bytes) {
    const n = Number(bytes) || 0;
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    return (n / 1024 / 1024).toFixed(1) + ' MB';
}

const TEMPLATES = [
    { key: 'std', template: '{kw}-{n:02}-{type}', sample: '星海中心-01-照片.jpg', desc: '混合格式推荐' },
    { key: 'simple', template: '{kw}-{n:02}', sample: '星海中心-01.jpg', desc: '最简洁' },
    { key: 'keep', template: '{kw}-{orig}', sample: '星海中心-方案汇报 v3.pptx', desc: '保留原名' },
    { key: 'dated', template: '{kw}-{date}-{n:02}', sample: '星海中心-20261018-01.jpg', desc: '按时间排序' },
];

function templateByKey(key) {
    for (let i = 0; i < TEMPLATES.length; i += 1) {
        if (TEMPLATES[i].key === key) return TEMPLATES[i];
    }
    return TEMPLATES[0];
}

/**
 * 套餐价格文案：990 分 / 30 天 → ¥9.9/月，9900 分 / 365 天 → ¥99/年。
 * 按天数判断周期，避免在数据里再存一个周期字段。
 */
function planPriceText(priceCents, days) {
    const yuan = Number(priceCents) / 100;
    const price = '¥' + (Math.round(yuan * 100) % 100 === 0
        ? String(Math.round(yuan))
        : yuan.toFixed(1));
    return price + (Number(days) >= 300 ? '/年' : '/月');
}

function dateText(epochSeconds) {
    const n = Number(epochSeconds);
    if (!n) return '';
    const d = new Date(n * 1000);
    const month = d.getMonth() + 1;
    const day = d.getDate();
    return d.getFullYear() + '-' + (month < 10 ? '0' + month : month) +
        '-' + (day < 10 ? '0' + day : day);
}

module.exports = {
    extOf: extOf,
    typeOf: typeOf,
    badgeOf: badgeOf,
    sizeText: sizeText,
    TEMPLATES: TEMPLATES,
    templateByKey: templateByKey,
    planPriceText: planPriceText,
    dateText: dateText,
};
