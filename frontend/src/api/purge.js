import {request,post} from './client.js';
export const purges={list:()=>request('/purges'),get:id=>request(`/purges/${id}`),create:(study_id,source_keys)=>post('/purges',{study_id,source_keys}),retry:id=>post(`/purges/${id}/retry`,{})};
