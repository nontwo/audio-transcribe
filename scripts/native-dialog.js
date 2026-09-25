// Static JXA source. Filenames/profile data are argv JSON, never executable source.
function naturalCompare(a, b) {
    var left = a.split('/').pop().toLowerCase().match(/[0-9]+|[^0-9]+/g) || [];
    var right = b.split('/').pop().toLowerCase().match(/[0-9]+|[^0-9]+/g) || [];
    for (var i = 0; i < Math.min(left.length, right.length); i++) {
        var ln = /^[0-9]+$/.test(left[i]), rn = /^[0-9]+$/.test(right[i]);
        if (ln && rn) {
            var x = left[i].replace(/^0+/, '') || '0', y = right[i].replace(/^0+/, '') || '0';
            if (x.length !== y.length) return x.length - y.length;
            if (x !== y) return x < y ? -1 : 1;
        } else if (left[i] !== right[i]) {
            return left[i] < right[i] ? -1 : 1;
        }
    }
    if (left.length !== right.length) return left.length - right.length;
    return a === b ? 0 : a < b ? -1 : 1;
}

function correctOrder(paths, answer) {
    var parts = answer.trim().split(/[\s,]+/);
    if (parts.length !== paths.length) return null;
    var seen = {}, result = [];
    for (var i = 0; i < parts.length; i++) {
        if (!/^[1-9][0-9]*$/.test(parts[i])) return null;
        var row = Number(parts[i]);
        if (row > paths.length || seen[row]) return null;
        seen[row] = true;
        result.push(paths[row - 1]);
    }
    return result;
}

function run(argv) {
    var app = Application.currentApplication();
    app.includeStandardAdditions = true;
    var data = JSON.parse(argv[0]);
    try {
        if (data.action === 'pick-only') {
            var only = app.chooseFile({withPrompt: 'Select a WAV recording', ofType: ['wav'], multipleSelectionsAllowed: false});
            return JSON.stringify({files: [only.toString()]});
        }
        var picked = app.chooseFile({withPrompt: 'AudioTranscribe: select existing WAV recordings', ofType: ['wav'], multipleSelectionsAllowed: true});
        var ordered = picked.map(function(p) { return p.toString(); }).sort(naturalCompare);
        while (true) {
            var preview = ordered.map(function(path, i) { return (i + 1) + '. ' + path; }).join('\n');
            var orderAnswer = app.displayDialog('Confirm source order:\n\n' + preview + '\n\nProposed order uses natural filenames, not selection-click order. Each source keeps its own timestamps.',
                {buttons: ['Cancel', 'Change order', 'Use order'], defaultButton: 'Use order', cancelButton: 'Cancel'});
            if (orderAnswer.buttonReturned === 'Use order') break;
            var input = app.displayDialog('Enter current row numbers in the desired order, once each (example: 2,1,3).',
                {defaultAnswer: ordered.map(function(_, i) { return i + 1; }).join(','), buttons: ['Cancel', 'Apply'], defaultButton: 'Apply', cancelButton: 'Cancel'}).textReturned;
            var corrected = correctOrder(ordered, input);
            if (corrected) ordered = corrected;
            else app.displayDialog('Invalid order. Include every displayed row number exactly once.', {buttons: ['OK']});
        }
        var selected = {speaker: null, capture: null, glossary: null}, created = [];
        var profileMode = app.displayDialog('Optional profiles: Speaker and capture unassigned; glossary empty. These defaults are reset for every selection.',
            {buttons: ['Cancel', 'Choose profiles', 'Use defaults'], defaultButton: 'Use defaults', cancelButton: 'Cancel'});
        if (profileMode.buttonReturned === 'Choose profiles') ['speaker', 'capture', 'glossary'].forEach(function(kind) {
            var profiles = data.profiles[kind];
            var choices = ['Unassigned'];
            if (kind !== 'glossary') choices.push('Create a new manually named profile');
            profiles.forEach(function(p) { choices.push(p.id + (p.label ? ' — ' + p.label : '')); });
            var answer = app.chooseFromList(choices, {withPrompt: 'Optional ' + kind + ' profile. Every new selection starts unassigned.', defaultItems: ['Unassigned'], multipleSelectionsAllowed: false});
            if (!answer) throw new Error('cancelled');
            if (answer[0] === 'Unassigned') {
                selected[kind] = null;
            } else if (answer[0] === 'Create a new manually named profile') {
                var id = app.displayDialog('Stable profile ID: 1–96 letters, digits, hyphens or underscores; begin with a letter or digit.', {defaultAnswer: '', buttons: ['Cancel', 'Create'], defaultButton: 'Create', cancelButton: 'Cancel'}).textReturned;
                if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$/.test(id)) throw new Error('invalid_profile_id');
                var label = app.displayDialog('Optional display label (no voice matching or training):', {defaultAnswer: id, buttons: ['Cancel', 'Continue'], defaultButton: 'Continue', cancelButton: 'Cancel'}).textReturned;
                selected[kind] = id;
                created.push({kind: kind, id: id, label: label});
            } else {
                selected[kind] = profiles[choices.indexOf(answer[0]) - (kind === 'glossary' ? 1 : 2)].id;
            }
        });
        var summary = 'Process or reuse ' + ordered.length + ' WAV file(s), in the confirmed order, then create ONE transcript-report.md containing their complete transcripts.\nSpeaker: ' + (selected.speaker || 'Unassigned') + '\nCapture: ' + (selected.capture || 'Unassigned') + '\nGlossary: ' + (selected.glossary || 'Empty') + '\nFiles are processed separately. Failed inputs remain visible in a partial report. Accuracy review remains pending.';
        app.displayDialog(summary, {buttons: ['Cancel', 'Transcribe'], defaultButton: 'Transcribe', cancelButton: 'Cancel'});
        return JSON.stringify({files: ordered, profiles: selected, new_profiles: created, order_confirmed: true});
    } catch (error) {
        if (error.errorNumber === -128 || error.message === 'cancelled') return JSON.stringify({cancelled: true});
        if (error.message === 'invalid_profile_id') {
            app.displayDialog('Invalid profile ID. Nothing was imported. Use letters, digits, hyphens or underscores.', {buttons: ['OK']});
            return JSON.stringify({cancelled: true, reason: 'invalid_profile_id'});
        }
        throw error;
    }
}
