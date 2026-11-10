() => {
    const found = new Set(), roots = [document], seenRoots = new Set();
    const extensions = /\.(mp4|mkv|webm|mov|m4v|m3u8|mpd|ogv|ogg|ts|mp3|aac|wav|flv|avi)(?:[?#]|$)/i;
    const attributes = ['src', 'href', 'data', 'data-src', 'data-video', 'data-url',
        'data-file', 'data-hd', 'data-sd', 'data-hls', 'data-dash', 'data-stream'];
    let nodes = 0, values = 0;
    function add(value, base, mediaContext = false) {
        if (typeof value !== 'string' || !value.trim() || found.size >= 2000) return;
        try {
            const url = new URL(value.trim(), base);
            if (!/^https?:$/.test(url.protocol)) return;
            if (!mediaContext && !extensions.test(url.href)) return;
            url.hash = '';
            found.add(url.href);
        } catch (_) {}
    }
    function json(text, base) {
        if (!text || text.length > 1000000) return;
        try {
            const stack = [{value:JSON.parse(text), depth:0, media:false}];
            while (stack.length && values++ < 10000) {
                const {value, depth, media} = stack.pop();
                if (typeof value === 'string') add(value, base, media);
                else if (value && typeof value === 'object' && depth < 20) {
                    const typed = /^(video|audio)/i.test(String(value.type || value['@type'] || ''));
                    for (const [key, item] of Object.entries(value).slice(0, 1000)) {
                        stack.push({value:item, depth:depth+1,
                            media: /^(contentUrl|streamUrl|hls|dash)$/i.test(key) ||
                                (typed && /^(url|src|file)$/i.test(key))});
                    }
                }
            }
        } catch (_) {}
    }
    while (roots.length && nodes < 20000) {
        const root = roots.pop();
        if (seenRoots.has(root)) continue;
        seenRoots.add(root);
        const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
        let el;
        while ((el = walker.nextNode()) && nodes++ < 20000) {
            const tag = el.tagName.toUpperCase();
            const base = /^https?:/.test(el.baseURI || '') ? el.baseURI : document.baseURI;
            const mediaElement = /^(VIDEO|AUDIO)$/.test(tag) ||
                (tag === 'SOURCE' && /^(VIDEO|AUDIO)$/.test(el.parentElement?.tagName || ''));
            for (const attr of attributes) {
                const mediaAttr = mediaElement && /^(src|data-src|data-file|data-url)$/.test(attr);
                add(el.getAttribute(attr), base, mediaAttr);
            }
            if (mediaElement) add(el.currentSrc, base, true);
            for (const attr of ['data-setup', 'data-config', 'data-player', 'data-sources'])
                json(el.getAttribute(attr), base);
            if (tag === 'SCRIPT' && /^(application\/(ld\+)?json)$/i.test(el.type || ''))
                json(el.textContent, base);
            if (el.shadowRoot) roots.push(el.shadowRoot);
            if (tag === 'TEMPLATE' && el.content) roots.push(el.content);
        }
    }
    for (const entry of performance.getEntriesByType('resource').slice(0, 2000))
        add(entry.name, document.baseURI);
    if (window.__mediaDiscovery) {
        for (const record of window.__mediaDiscovery.snapshot().slice(0, 2000))
            add(record.url, document.baseURI, true);
    }
    return [...found];
}
