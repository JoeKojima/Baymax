"""Reproducible synthetic persona evaluation. Never uses the real memory space."""
import argparse
from collections import Counter
import html
import json
from pathlib import Path
import tempfile
import time

from .graph import PalaceGraph


def average_link_clusters(similarities, threshold=0.40):
    """Label-blind average-link cosine clustering; no fixed cluster count."""
    groups = [[i] for i in range(len(similarities))]
    while len(groups) > 1:
        choices = [(sum(float(similarities[a][b]) for a in left for b in right) / (len(left)*len(right)), i, j)
                   for i,left in enumerate(groups) for j,right in enumerate(groups) if j > i]
        score, i, j = max(choices)
        if score < threshold:
            break
        groups[i] += groups.pop(j)
    return groups


def evaluate(fixture, embedder, threshold=0.40, encoder=None):
    import numpy as np
    rows = fixture['utterances']
    if fixture.get('synthetic') is not True:
        raise ValueError('This harness only accepts explicitly synthetic fixtures.')
    with tempfile.TemporaryDirectory(prefix='ember-synthetic-persona-') as folder:
        graph = PalaceGraph(Path(folder)/'memory-palace.sqlite3', encoder=encoder)
        graph.ingest([{'t':i+1,'speaker':'user','text':row['text']} for i,row in enumerate(rows)])
        for _ in rows:
            graph.step()
        snapshot = graph.snapshot()
    for row in rows:
        row['encoded'] = [c['value'] for c in snapshot['claims']
                          if any(e['quote'] in row['text'] for e in c['evidence'])]
    start=time.perf_counter()
    vectors=np.asarray(embedder.embed([r['text'] for r in rows]),dtype=float)
    vectors /= np.maximum(np.linalg.norm(vectors,axis=1,keepdims=True),1e-12)
    similarities=vectors @ vectors.T
    groups=average_link_clusters(similarities,threshold)
    centered=vectors-vectors.mean(axis=0)
    _,singular,axes=np.linalg.svd(centered,full_matrices=False)
    projection=centered @ axes[:2].T
    explained=float((singular[:2]**2).sum()/(singular**2).sum())
    queries=np.asarray(embedder.embed([q['text'] for q in fixture['queries']]),dtype=float)
    queries /= np.maximum(np.linalg.norm(queries,axis=1,keepdims=True),1e-12)
    search=queries @ vectors.T
    query_results=[]
    for q,scores in zip(fixture['queries'],search):
        top=np.argsort(-scores)[:3]
        query_results.append({**q,'top3':[{'id':rows[int(i)]['id'],'topic':rows[int(i)]['topic'],
                     'text':rows[int(i)]['text'],'cosine':round(float(scores[i]),4)} for i in top]})
    clusters=[]
    correct=0
    for i,indices in enumerate(groups):
        counts=Counter(rows[j]['topic'] for j in indices)
        correct+=max(counts.values())
        clusters.append({'id':i+1,'topics':dict(counts),'members':[rows[j]['id'] for j in indices]})
    nearest=[]
    for i,row in enumerate(rows):
        scores=similarities[i].copy();scores[i]=-2
        j=int(scores.argmax())
        nearest.append({'id':row['id'],'neighbor':rows[j]['id'],'cosine':round(float(scores[j]),4),
                        'same_topic':row['topic']==rows[j]['topic']})
    coverage={topic:{'captured':sum(bool(r['encoded']) for r in rows if r['topic']==topic),
                     'total':sum(r['topic']==topic for r in rows)} for topic in sorted({r['topic'] for r in rows})}
    return {'synthetic':True,'model':'all-MiniLM-L6-v2 / local ONNX','dimensions':int(vectors.shape[1]),
            'embedding_ms':round((time.perf_counter()-start)*1000,1),'threshold':threshold,
            'projection':projection.tolist(),'projection_explained_variance':round(explained,4),
            'utterances':rows,'extraction':coverage,'claims':snapshot['claims'],
            'clusters':clusters,'cluster_purity':round(correct/len(rows),3),
            'nearest_neighbor_topic_agreement':sum(n['same_topic'] for n in nearest)/len(rows),
            'nearest':nearest,'queries':query_results,
            'query_top1_topic_matches':sum(q['topic']==q['top3'][0]['topic'] for q in query_results),
            'note':'Topic labels are used only to score results, never to form clusters. Threshold chosen before this run. '
                   'All utterances are embedded, including statements the rule encoder misses. Similarity is not truth or verified identity. '
                   'The inputs include questions and episodic remarks that should not all become durable personal facts; extraction coverage is not an extraction recall score. '
                   'This is one small synthetic diagnostic, not a general accuracy benchmark or live graph integration.'}


def write_report(result,path):
    esc=lambda v:html.escape(str(v))
    sections=[]
    for topic,counts in result['extraction'].items():
        rows=[r for r in result['utterances'] if r['topic']==topic]
        items=''.join('<tr><td>'+esc(r['text'])+'</td><td>'+esc('; '.join(r['encoded']) or 'No proposal extracted')+'</td></tr>' for r in rows)
        sections.append('<h2>'+esc(topic)+' — '+str(counts['captured'])+'/'+str(counts['total'])+' utterances extracted</h2><table><tr><th>Synthetic utterance</th><th>Actual encoded value</th></tr>'+items+'</table>')
    by_id={r['id']:r for r in result['utterances']}
    clusters=''.join('<section><h3>Cluster '+str(c['id'])+'</h3><p>'+esc(c['topics'])+'</p><ul>'+''.join('<li>'+esc(by_id[i]['text'])+'</li>' for i in c['members'])+'</ul></section>' for c in result['clusters'])
    queries=''.join('<h3>'+esc(q['text'])+'</h3><ol>'+''.join('<li>'+esc(r['text'])+' <small>cosine '+str(r['cosine'])+'</small></li>' for r in q['top3'])+'</ol>' for q in result['queries'])
    document='''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Ember synthetic memory test</title><style>body{font:16px/1.6 system-ui;background:#171c19;color:#e9ede8;max-width:1100px;margin:32px auto;padding:0 24px}h1,h2{color:#ffb783}table{width:100%;border-collapse:collapse}td,th{padding:12px;text-align:left;border-bottom:1px solid #415247;vertical-align:top}th{color:#b4c1b7}section{border-left:2px solid #415247;padding-left:20px;margin:24px 0}small,p{color:#b4c1b7}</style><h1>Synthetic memory laboratory</h1><p>Invented persona only. No real memories were changed. All vectors were computed locally.</p>'''
    document+='<p>Encoder: '+esc(result.get('encoder','local rules'))+'; Gemini test requests: '+str(result.get('gemini_requests',0))+'</p>'
    document+='<p>'+esc(result['note'])+'</p><h2>Measured summary</h2><p>'+esc({k:result[k] for k in ('dimensions','embedding_ms','threshold','cluster_purity','nearest_neighbor_topic_agreement','query_top1_topic_matches')})+'</p>'
    document+='<h2>Vector space projection</h2><p>Each point is a synthetic utterance. Colors are intended topics for interpretation, not inputs to clustering. Distances are a lossy 2D projection; grouping uses the full 384 dimensions.</p><img src="vector-space.svg" alt="Two-dimensional PCA projection of the synthetic utterance embeddings" style="width:100%;height:auto">'
    document+=''.join(sections)+'<h2>Unsupervised similarity groups</h2>'+clusters+'<h2>Semantic retrieval probes</h2>'+queries
    path.write_text(document,encoding='utf-8')


def write_plot(result,folder):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig,ax=plt.subplots(figsize=(10,6),layout='constrained')
    points=result['projection']
    for topic in result['extraction']:
        ids=[i for i,r in enumerate(result['utterances']) if r['topic']==topic]
        ax.scatter([points[i][0] for i in ids],[points[i][1] for i in ids],label=topic,s=65)
        for i in ids:
            ax.annotate(result['utterances'][i]['id'].rsplit('-',1)[1],points[i],xytext=((8,8),(8,-15),(-16,8),(-16,-15))[i%4],textcoords='offset points',fontsize=9)
    ax.set(title='Synthetic Ember persona: local sentence embeddings',xlabel='Principal component 1',ylabel='Principal component 2')
    ax.text(0,-.19,f"PCA preserves {result['projection_explained_variance']:.0%} of variance here. Numbers identify utterances within each topic. Clustering uses all 384 dimensions.",transform=ax.transAxes,fontsize=9)
    ax.legend(title='Intended topics (evaluation labels)',loc='upper left',bbox_to_anchor=(1,1))
    fig.savefig(folder/'vector-space.svg')
    fig.savefig(folder/'vector-space.png',dpi=150)
    plt.close(fig)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    root=Path(__file__).resolve().parents[2]
    parser.add_argument('--fixture',type=Path,default=root/'tests/fixtures/memory_persona.json')
    parser.add_argument('--model-dir',type=Path,default=root/'all-MiniLM-L6-v2-onnx')
    parser.add_argument('--output',type=Path,default=root/'.local-review/persona-evaluation')
    args=parser.parse_args()
    from semantic_embedder import SemanticEmbedder
    result=evaluate(json.loads(args.fixture.read_text(encoding='utf-8')),SemanticEmbedder(model_dir=args.model_dir,chroma_dir=None,verbose=False))
    args.output.mkdir(parents=True,exist_ok=True)
    (args.output/'results.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    write_report(result,args.output/'report.html')
    write_plot(result,args.output)
    print(json.dumps({k:result[k] for k in ('extraction','dimensions','embedding_ms','cluster_purity','nearest_neighbor_topic_agreement','query_top1_topic_matches')}))
    print('Clusters: '+json.dumps(result['clusters']))

if __name__=='__main__':main()
