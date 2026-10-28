"""Single-file analyst benchmark with independent expected facts and optional live AI."""
import argparse
import csv
import io
import json
import sys
import tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from reviso.agent import Trace, generate_report, chat
from reviso.analysis import build_report
from reviso.data import Store, now
from reviso.server import export_html


def fixture():
    stream=io.StringIO();writer=csv.writer(stream)
    writer.writerow(['record_id','price','area','region','quality','sale_date','description'])
    for i in range(1,31):
        writer.writerow([i,10000 if i==30 else i*10,5000 if i==30 else i*5,'North' if i%2 else 'South','Low' if i%2 else 'High',f'2026-01-{i:02d}','' if i==3 else 'A sample property with an original description and details for follow-up questions.'])
    return stream.getvalue().encode()


def evaluate(report):
    e=report['exploration']; fields=e['fields']
    checks={
        'one file without invented comparisons':len(report['versions'])==1 and not report['comparisons'] and report['baseline'] is None,
        '30 source records':e['records']==30,
        '7 source fields':len(fields)==7,
        '1 missing description':fields['description']['missing']==1,
        '1 unusual price':fields['price']['unusual_values']==1,
        'price median 155':fields['price']['median']==155,
        'rating is ordinal':fields['quality']['role']=='ordinal',
        'region is nominal':fields['region']['role']=='nominal',
        'two price groups':any(g['field']=='region' and g['available_groups']==2 for g in e['group_summaries']),
        '30 valid time values':e['time_summary']['valid_dates']==30,
        'perfect price-area association':any(r['pearson_r']==1 and {r['left'],r['right']}=={'price','area'} for r in e['relationships']),
        '1 record with several unusual measures':any(f['kind']=='record_screen' and f['metrics']['records']==1 for f in report['findings']),
    }
    return {'passed':sum(checks.values()),'total':len(checks),'checks':checks}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--provider',choices=['offline','ollama','gemini','openai','groq'],default='offline')
    parser.add_argument('--model',default=None)
    parser.add_argument('--effort',choices=['auto','low','high'],default='auto')
    parser.add_argument('--context',type=int,default=16384)
    parser.add_argument('--output',type=Path,default=Path('outputs/single-evaluation.json'))
    args=parser.parse_args();options={'effort':args.effort,'context_window':args.context}
    with tempfile.TemporaryDirectory(prefix='analytic-brain-single-') as directory:
        store=Store(Path(directory)/'test.sqlite');identifier=store.create_dataset('Single-file property analysis')['id']
        store.ingest(identifier,'One file',fixture())
        store.configure(identifier,{'setup_complete':True,'dataset_type':'prices','identity':['record_id'],'important':['price','area'],
                                    'target_column':'price','date_column':'sale_date','analysis_goal':'Inspect prices, regions, and unusual records.',
                                    'row_meaning':'One property sale; prices and area use consistent sample units.',
                                    'cloud_allowed':args.provider not in ('offline','ollama')})
        measurements=evaluate(build_report(store,identifier))
        output={'created_at':now(),'provider':args.provider,'model':args.model,'measurements':measurements,'answers':[],
                'ai_semantic_quality':'Pending human review; execution and evidence guards are separate from correctness.'}
        trace=Trace(store,identifier,'single_report',args.provider,args.model)
        try:
            result=generate_report(store,identifier,trace,args.provider,args.model,options=options);trace.finish(result)
            report=store.report(identifier);output['report']=report
            if args.provider!='offline':
                for question in ('What is the highest price, and which source record has it?', 'Which fields have missing values, and what should I verify?'):
                    run=Trace(store,identifier,'single_chat',args.provider,args.model,question)
                    try:
                        answer=chat(store,identifier,question,run,args.provider,args.model,options=options)
                        run.finish(answer);output['answers'].append({'question':question,'answer':answer})
                    except ValueError as exc:
                        run.finish(error=exc);output['answers'].append({'question':question,'error':str(exc)})
        except ValueError as exc:
            trace.finish(error=exc);output['error']=str(exc)
        output['runs']=store.runs(identifier)
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(output,indent=2,ensure_ascii=False),encoding='utf-8')
        if output.get('report'):args.output.with_suffix('.html').write_text(export_html(output['report']),encoding='utf-8')
        failures=int('error' in output)+sum('error' in a for a in output['answers'])
        print(f"Single-file measurements: {measurements['passed']}/{measurements['total']}. AI execution failures: {failures}. Saved {args.output}")
        if failures or measurements['passed']!=measurements['total']:sys.exit(1)

if __name__=='__main__':main()
