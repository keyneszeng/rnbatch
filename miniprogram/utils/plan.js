/**
 * 演示模式的本地改名计划器。
 *
 * 这是 rnbatch 引擎 plan() 的 JS 简化版，只做演示用：模板渲染、补零、
 * 同批次去重、与已有文件名比对冲突。真实的改名仍然由服务端 Python 引擎执行，
 * 因为两阶段改名和撤销日志必须落在文件系统那一侧。
 */
const fmt = require('./format');

const ILLEGAL = /[\/\\:*?"<>|\x00-\x1f]/g;
const MAX_NAME_BYTES = 200;

function sanitize(value) {
    return String(value || '')
        .replace(ILLEGAL, '')
        .replace(/\s+/g, ' ')
        .replace(/^[ .]+/, '')
        .replace(/[ .]+$/, '');
}

function pad(number, width) {
    let out = String(number);
    while (out.length < width) out = '0' + out;
    return out;
}

function utf8Len(text) {
    let total = 0;
    for (let i = 0; i < text.length; i += 1) {
        const code = text.charCodeAt(i);
        if (code < 0x80) {
            total += 1;
        } else if (code < 0x800) {
            total += 2;
        } else if (code >= 0xd800 && code <= 0xdbff) {
            total += 4;
            i += 1;
        } else {
            total += 3;
        }
    }
    return total;
}

function fitLength(name, ext) {
    const budget = MAX_NAME_BYTES - utf8Len(ext);
    const cap = budget < 1 ? MAX_NAME_BYTES : budget;
    let out = name;
    while (utf8Len(out) > cap && out.length > 0) {
        out = out.slice(0, -1);
        const last = out.charCodeAt(out.length - 1);
        if (last >= 0xd800 && last <= 0xdbff) out = out.slice(0, -1);
    }
    out = out.replace(/[ .]+$/, '');
    return out || 'renamed';
}

function splitExt(name) {
    const dot = String(name).lastIndexOf('.');
    if (dot <= 0) return { root: String(name), ext: '' };
    return { root: String(name).slice(0, dot), ext: String(name).slice(dot) };
}

function buildName(template, ctx) {
    let name = String(template || '');
    name = name.replace(/\{n:0?(\d+)d?\}/g, function (match, width) {
        return pad(ctx.index, parseInt(width, 10));
    });
    name = name.replace(/\{n\}/g, pad(ctx.index, ctx.width));
    name = name.replace(/\{kw\}/g, ctx.keyword);
    name = name.replace(/\{orig\}/g, ctx.orig);
    name = name.replace(/\{date\}/g, ctx.date);
    name = name.replace(/\{type\}/g, ctx.type);

    if (name.indexOf('{ext}') >= 0) {
        const bare = ctx.ext.replace(/^\./, '');
        name = bare
            ? name.replace(/[-_. ]?\{ext\}/g, '.' + bare)
            : name.replace(/[-_. ]?\{ext\}/g, '');
    }
    return sanitize(name);
}

function ensureExt(name, ext) {
    if (!ext) return name;
    const tail = name.slice(name.length - ext.length);
    return tail.toLowerCase() === ext.toLowerCase() ? name : name + ext;
}

function today() {
    const now = new Date();
    const month = pad(now.getMonth() + 1, 2);
    const day = pad(now.getDate(), 2);
    return String(now.getFullYear()) + month + day;
}

function buildPlan(files, keyword, template) {
    const kw = sanitize(keyword);
    if (!kw) throw new Error('关键词不能为空');

    const ordered = files.slice().sort(function (a, b) {
        const left = a.name.toLowerCase();
        const right = b.name.toLowerCase();
        if (left === right) return 0;
        return left < right ? -1 : 1;
    });
    const width = Math.max(2, String(ordered.length).length);
    const taken = {};
    const rows = [];

    for (let i = 0; i < ordered.length; i += 1) {
        const file = ordered[i];
        const parts = splitExt(file.name);
        const ext = parts.ext;
        let base = buildName(template, {
            index: i + 1,
            width: width,
            keyword: kw,
            orig: parts.root,
            date: file.date || today(),
            type: fmt.typeOf(file.name),
            ext: ext,
        });
        if (!base) base = kw + '-' + pad(i + 1, width);

        const full = ensureExt(base, ext);
        const fullParts = splitExt(full);
        const root = fitLength(fullParts.root, fullParts.ext);
        let candidate = root + fullParts.ext;

        if (taken[candidate.toLowerCase()]) {
            let k = 2;
            while (k < 1000) {
                const alt = fitLength(root + '-' + k, fullParts.ext) + fullParts.ext;
                if (!taken[alt.toLowerCase()]) {
                    candidate = alt;
                    break;
                }
                k += 1;
            }
        }
        taken[candidate.toLowerCase()] = true;
        rows.push({
            from: file.name,
            to: candidate,
            changed: file.name !== candidate,
            conflict: false,
        });
    }

    const sources = {};
    const existing = {};
    for (let i = 0; i < files.length; i += 1) existing[files[i].name.toLowerCase()] = true;
    for (let i = 0; i < rows.length; i += 1) sources[rows[i].from.toLowerCase()] = true;
    for (let i = 0; i < rows.length; i += 1) {
        if (rows[i].from === rows[i].to) continue;
        const key = rows[i].to.toLowerCase();
        if (existing[key] && !sources[key]) rows[i].conflict = true;
    }

    let changed = 0;
    let conflicts = 0;
    for (let i = 0; i < rows.length; i += 1) {
        if (rows[i].changed) changed += 1;
        if (rows[i].conflict) conflicts += 1;
    }
    return { rows: rows, changed: changed, conflicts: conflicts };
}

module.exports = {
    buildPlan: buildPlan,
    sanitize: sanitize,
};
