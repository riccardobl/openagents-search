from openagents import JobRunner,OpenAgentsNode,NodeConfig,RunnerConfig,JobContext

import base64
import json
import numpy as np
import faiss
import hashlib
import asyncio
import time
import os
import gc


class SearchRunner (JobRunner):
   

    def __init__(self):
        super().__init__(   
            RunnerConfig(
                meta={
                    "kind": 5003,
                    "name": "Vector Search",
                    "description": "Perform similarity search given some passages and queries embeddings",
                    "tos": "https://openagents.com/terms",
                    "privacy": "https://openagents.com/privacy",
                    "author": "OpenAgentsInc",
                    "web": "https://github.com/OpenAgentsInc/openagents-search",
                    "picture": "",
                    "tags": [
                        "embeddings-search"
                    ],
                },
                filter={"filterByRunOn": "openagents\\/search"},
                template="""{
                    "kind": {{{meta.kind}}},
                    "created_at": {{{sys.timestamp_seconds}}},
                    "tags": [
                        ["param","run-on", "openagents/search" ],                             
                        ["param", "k", "{{{in.k}}}"],
                        ["param", "normalize", "{{{in.normalize}}}"],
                        ["output", "{{{in.outputType}}}"],
                        {{#in.queries}}
                        ["i", "{{value}}", "{{{type}}}", "",  "query"],
                        {{/in.queries}}
                        {{#in.indices}}
                        ["i", "{{value}}", "{{{type}}}", "",  "index"],
                        {{/in.indices}}
                        ["expiration", "{{{sys.expiration_timestamp_seconds}}}"]
                    ],
                    "content":""
                }
                """,
                sockets={
                    "in":{
                        "k":{
                            "title": "K",
                            "description": "The number of embeddings to return",
                            "type": "integer",
                            "default": 4
                        },
                        "normalize":{
                            "title": "Normalize",
                            "description": "Normalize index",
                            "type": "boolean",
                            "default": True
                        },
                        "queries":{
                            "title": "Queries",
                            "description": "The queries",
                            "type":"array",
                            "items":{
                                "type":"map",
                                "properties":{
                                    "value":{
                                        "title":"Value",
                                        "description":"Stringified JSON object or hyperdrive with the query embedding",
                                        "type":"string"
                                    },
                                    "type":{
                                        "title":"Type",
                                        "description":"The input type of the query. Either application/json or application/hyperdrive+bundle",
                                        "type":"string",
                                        "default":"application/json"
                                    }
                                }
                            }
                        },
                        "indices":{
                            "title": "Indices",
                            "description": "The index to search on",
                            "type":"array",
                            "items":{
                                "type":"map",
                                "properties":{
                                    "value":{
                                        "title":"Value",
                                        "description":"Stringified JSON object or hyperdrive with the document embedding",
                                        "type":"string"
                                    },
                                    "type":{
                                        "title":"Type",
                                        "description":"The input type of the document. Either application/json or application/hyperdrive+bundle",
                                        "type":"string",
                                        "default":"application/json"
                                    }
                                }
                            }
                        },
                        "outputType":{
                            "title": "Output Type",
                            "description": "The Desired Output Type",
                            "type": "string",
                            "default": "application/json"
                        },
                    },
                    "out":{
                        "output":{
                            "title": "Output",
                            "description": "The top K embeddings encoded in json or an hyperdrive bundle url",
                            "type": "string"
                        }
                    }
                }

            )
        )
        self.INDEXES={}
        self.SEARCH_QUEUE = []
        self.MAX_MEMORY_CACHE_GB = 1        
        self.MAX_MEMORY_CACHE_GB = float(os.getenv('SEARCH_MAX_MEMORY_CACHE_GB', self.MAX_MEMORY_CACHE_GB))
        self.setRunInParallel(True)

    async def deserializeFromBlob(self,  url,  out_vectors , out_content,ctx:JobContext):
        logger=ctx.getLogger()

        disk = await ctx.openStorage( url)
        logger.log("Reading embeddings from "+url)
        
        # Find embeddings files
        sentencesIn = await disk.openReadStream("sentences.bin")
        embeddingsIn = await disk.openReadStream("embeddings.bin")

        dtype = None
        shape = None
         
        nSentences = await sentencesIn.readInt()
        for i in range(nSentences):
            logger.log("Reading sentence "+str(i))
            lenSentence = await sentencesIn.readInt()
            sentence = await sentencesIn.read(lenSentence)
            sentence=sentence.decode()
            out_content.append(sentence)

        nEmbeddings = await embeddingsIn.readInt()
        for i in range(nEmbeddings):
            logger.log("Reading embeddings "+str(i))
            shape = []
            lenShape = await embeddingsIn.readInt()
            for j in range(lenShape):
                shape.append(await embeddingsIn.readInt())

            lenDtype = await embeddingsIn.readInt()
            dtype = (await embeddingsIn.read(lenDtype)).decode()

            lenBs = await embeddingsIn.readInt()
            bs = await embeddingsIn.read(lenBs)
            embeddings = np.frombuffer(bs, dtype=dtype).reshape(shape)
            out_vectors.append(embeddings)
                
 

        await disk.close()
        return [dtype,shape]

    async def deserializeFromJSON( self, data,  out_vectors ,out_content, ctx:JobContext):
        logger=ctx.getLogger()
        logger.log("Reading embeddings from JSON")
        dtype=None
        shape=None
        data=json.loads(data)
        for part in data:
            text = part[0]
            embeddings_b64 = part[1]
            _dtype = part[2]
            _shape = part[3]
            if dtype is None: dtype = _dtype
            elif dtype != _dtype: raise Exception("Data type mismatch")
            if shape is None: shape = _shape
            elif shape != _shape: raise Exception("Shape mismatch")
            embeddings_bytes = base64.b64decode(embeddings_b64)
            embeddings =  np.frombuffer(embeddings_bytes, dtype=dtype).reshape(shape)
            out_vectors.append(embeddings)
            out_content.append(text)
        return [dtype,shape]

    async def deserialize( self, jin,out_vectors ,out_content,ctx:JobContext):
        dtype = None
        shape = None
        data = jin.data
        dataType = jin.type
        marker = jin.marker   
        if dataType == "application/hyperdrive+bundle":
            [dtype,shape] = await self.deserializeFromBlob(data, out_vectors, out_content, ctx)
        else:
            [dtype,shape] =  await self.deserializeFromJSON(data, out_vectors, out_content, ctx)
        return [dtype,shape]


    async def loop(self, node:OpenAgentsNode):     
        logger=node.getLogger()

        if len(self.SEARCH_QUEUE) == 0:
            await asyncio.sleep(10.0/1000.0)
            return
        search = self.SEARCH_QUEUE.pop(0)

        faiss_index = search["faiss_index"]
        queue = search["queue"]
        top_k = 1
        
        flattern_queries = []
        flattern_queries_idInQueue = []
        for i in range(len(queue)):
            enqueued = queue[i]
            enqueued_top_k = enqueued[1]
            for query in enqueued[0]:
                flattern_queries.append(query)
                flattern_queries_idInQueue.append(i)
            if enqueued_top_k > top_k:
                top_k = enqueued_top_k
        
        if len(flattern_queries) == 0:
            return
        logger.info("Searching "+str(len(flattern_queries))+" queries")
        flattern_queries=np.array(flattern_queries)
        distances, indices = faiss_index.search(flattern_queries, top_k)
        for i in range(len(queue)):
            distances_for_entry = []
            indices_for_entry = []
            for j in range(len(flattern_queries)):
                if flattern_queries_idInQueue[j] == i:
                    distances_for_entry.append(distances[j])
                    indices_for_entry.append(indices[j])
            queue[i][2](distances_for_entry, indices_for_entry)
        await asyncio.sleep(10.0/1000.0)
        
        

    async def run(self,ctx):
       
        logger=ctx.getLogger()
        job=ctx.getJob()
        # Extract parameters
        top_k = int(ctx.getJobParamValue("k", "4"))
        normalize = str(ctx.getJobParamValue("normalize", "true"))=="true"
        
        # Deserialize inputs
        indexId=""
        for jin in job.input:
            marker = jin.marker
            if marker != "query":
                indexId += jin.data
        if len(indexId) == 0:
            logger.log("No index")
            return json.dumps([])
        indexId=hashlib.sha256(indexId.encode()).hexdigest() 
                
        index = self.INDEXES.get(indexId)
        if not index:
            logger.info("Loading index")
            index_vectors = []
            index_content = []
            dtype = None
            shape = None
            for jin in job.input:
                if jin.marker == "query":
                    continue
                [dtype,shape] = await self.deserialize(jin,index_vectors ,index_content, ctx)               

            logger.info("Preparing index")
            index_vectors = np.array(index_vectors)
            if normalize and dtype == "float32":
                faiss.normalize_L2(index_vectors)

            # Create faiss index
            logger.info("Creating faiss index")
            faiss_index = faiss.IndexFlatL2(shape[0])
            faiss_index.add(index_vectors)
            logger.log("Counting memory usage")
            indexSizeGB = faiss_index.ntotal * shape[0] * 4 / 1024 / 1024 / 1024
            index = [faiss_index, time.time(), index_content, indexSizeGB]
            self.INDEXES[indexId] = index

            logger.log("Dropping oldest indexes if out of memory limit")
            # drop oldest index if out of memory limit
            totalSize = sum([x[3] for x in self.INDEXES.values()])
            while totalSize > self.MAX_MEMORY_CACHE_GB and len(self.INDEXES) > 1:
                oldest = min(self.INDEXES.values(), key=lambda x: x[1])
                logger.log("Max cache size reached. Dropping oldest index.")
                del self.INDEXES[oldest]
                totalSize -= oldest[3]
            gc.collect()
        else:
            logger.info("Index already loaded")
        index[1] = time.time()

        logger.log("Preparing queries")
        queries = []
        for jin in job.input:
            if jin.marker == "query":
                logger.log("Preparing query")
                searches_vectors = []
                searches_content = [] 
                [dtype,shape] = await self.deserialize(jin, searches_vectors, searches_content,ctx)
                searches_vectors = np.array(searches_vectors)
                if normalize and dtype == "float32":
                    logger.log("Normalizing")
                    faiss.normalize_L2(searches_vectors)
                queries=searches_vectors
            
        queries = [ x for x in queries if len(x) > 0]

        if len(queries) == 0 :
            logger.log("No queries")
            return json.dumps([])
        
        # Search faiss index        
        logger.info("Searching")
        search = next((x for x in self.SEARCH_QUEUE if x["indexId"] == indexId), None)
        if not search:
            search = {
                "queue": [],
                "faiss_index": index[0],
                "indexId": indexId
            }
            self.SEARCH_QUEUE.append(search)
        queue = search["queue"]

        future =  asyncio.Future()
        def callback(distances, indices):
            # Get content for each search query and sort by score
            logger.info("Retrieving content from index")
            output_per_search = []
            index_content = index[2]
            for i in range(len(indices)):
                output_per_search.append([])
                for j in range(len(indices[i])):
                    content = index_content[indices[i][j]]
                    output_per_search[i].append({"value": content, "score": float(distances[i][j]), "contentId": str(indices[i][j])})
                output_per_search[i] = sorted( output_per_search[i], key=lambda x: x["score"], reverse=False)
                
            # Merge results from all searches 
            logger.info("Merging search results")
            output = []
            i=0
            while len(output) < len(output_per_search)*top_k:
                for j in range(len(output_per_search)):
                    if i < len(output_per_search[j]):
                        output.append(output_per_search[j][i])
                i+=1       

            # Remove duplicates
            logger.info("Deduplicating")
            dedup = []
            dedup_ids=[]
            for o in output:
                if o["contentId"] not in dedup_ids:
                    dedup.append(o)
                    dedup_ids.append(o["contentId"])
            output = dedup
            
            # truncate output
            
            output = output[:min(top_k, len(output))]
            future.set_result(output)

        logger.info("Waiting for search results")
        queue.append([
            queries,
            top_k,
            callback
        ])
        output = await future
        
        # Serialize output and return
        logger.info("Output ready")
        return json.dumps(output)


node = OpenAgentsNode(NodeConfig({
    "name": "OpenAgents Vector Search Service",
    "description": "Perform similarity search given some passages and queries embeddings",
    "version": "0.0.1",
}))
node.registerRunner(SearchRunner())
node.start()